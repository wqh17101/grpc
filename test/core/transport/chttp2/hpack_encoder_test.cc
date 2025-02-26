/*
 *
 * Copyright 2015 gRPC authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

#include "src/core/ext/transport/chttp2/transport/hpack_encoder.h"

#include <stdlib.h>
#include <string.h>

#include <memory>
#include <string>

#include "gmock/gmock.h"
#include "gtest/gtest.h"

#include <grpc/event_engine/memory_allocator.h>
#include <grpc/slice_buffer.h>
#include <grpc/support/log.h>

#include "src/core/ext/transport/chttp2/transport/frame.h"
#include "src/core/lib/gprpp/ref_counted_ptr.h"
#include "src/core/lib/iomgr/exec_ctx.h"
#include "src/core/lib/resource_quota/arena.h"
#include "src/core/lib/resource_quota/memory_quota.h"
#include "src/core/lib/resource_quota/resource_quota.h"
#include "src/core/lib/slice/slice_internal.h"
#include "test/core/util/parse_hexstring.h"
#include "test/core/util/slice_splitter.h"
#include "test/core/util/test_config.h"

static auto* g_memory_allocator = new grpc_core::MemoryAllocator(
    grpc_core::ResourceQuota::Default()->memory_quota()->CreateMemoryAllocator(
        "test"));

grpc_core::HPackCompressor* g_compressor;

typedef struct {
  bool eof;
  bool use_true_binary_metadata;
} verify_params;

/* verify that the output frames that are generated by encoding the stream
   have sensible type and flags values */
static void verify_frames(grpc_slice_buffer& output, bool header_is_eof) {
  /* per the HTTP/2 spec:
       All frames begin with a fixed 9-octet header followed by a
       variable-length payload.

       +-----------------------------------------------+
       |                 Length (24)                   |
       +---------------+---------------+---------------+
       |   Type (8)    |   Flags (8)   |
       +-+-------------+---------------+-------------------------------+
       |R|                 Stream Identifier (31)                      |
       +=+=============================================================+
       |                   Frame Payload (0...)                      ...
       +---------------------------------------------------------------+
   */
  uint8_t type = 0xff, flags = 0xff;
  size_t i, merged_length, frame_size;
  bool first_frame = false;
  bool in_header = false;
  bool end_header = false;
  bool is_closed = false;
  for (i = 0; i < output.count;) {
    first_frame = i == 0;
    grpc_slice* slice = &output.slices[i++];

    // Read gRPC frame header
    uint8_t* p = GRPC_SLICE_START_PTR(*slice);
    frame_size = 0;
    frame_size |= static_cast<uint32_t>(p[0]) << 16;
    frame_size |= static_cast<uint32_t>(p[1]) << 8;
    frame_size |= static_cast<uint32_t>(p[2]);
    type = p[3];
    flags = p[4];

    // Read remainder of the gRPC frame
    merged_length = GRPC_SLICE_LENGTH(*slice);
    while (merged_length < frame_size + 9) {  // including 9 byte frame header
      grpc_slice* slice = &output.slices[i++];
      merged_length += GRPC_SLICE_LENGTH(*slice);
    }

    // Verifications
    if (first_frame && type != GRPC_CHTTP2_FRAME_HEADER) {
      gpr_log(GPR_ERROR, "expected first frame to be of type header");
      gpr_log(GPR_ERROR, "EXPECT: 0x%x", GRPC_CHTTP2_FRAME_HEADER);
      gpr_log(GPR_ERROR, "GOT:    0x%x", type);
      EXPECT_TRUE(false);
    } else if (first_frame && header_is_eof &&
               !(flags & GRPC_CHTTP2_DATA_FLAG_END_STREAM)) {
      gpr_log(GPR_ERROR, "missing END_STREAM flag in HEADER frame");
      EXPECT_TRUE(false);
    }
    if (is_closed &&
        (type == GRPC_CHTTP2_FRAME_DATA || type == GRPC_CHTTP2_FRAME_HEADER)) {
      gpr_log(GPR_ERROR,
              "stream is closed; new frame headers and data are not allowed");
      EXPECT_TRUE(false);
    }
    if (end_header && (type == GRPC_CHTTP2_FRAME_HEADER ||
                       type == GRPC_CHTTP2_FRAME_CONTINUATION)) {
      gpr_log(GPR_ERROR,
              "frame header is ended; new headers and continuations are not "
              "allowed");
      EXPECT_TRUE(false);
    }
    if (in_header &&
        (type == GRPC_CHTTP2_FRAME_DATA || type == GRPC_CHTTP2_FRAME_HEADER)) {
      gpr_log(GPR_ERROR,
              "parsing frame header; new headers and data are not allowed");
      EXPECT_TRUE(false);
    }
    if (flags & ~(GRPC_CHTTP2_DATA_FLAG_END_STREAM |
                  GRPC_CHTTP2_DATA_FLAG_END_HEADERS)) {
      gpr_log(GPR_ERROR, "unexpected frame flags: 0x%x", flags);
      EXPECT_TRUE(false);
    }

    // Update state
    if (flags & GRPC_CHTTP2_DATA_FLAG_END_HEADERS) {
      in_header = false;
      end_header = true;
    } else if (type == GRPC_CHTTP2_DATA_FLAG_END_HEADERS) {
      in_header = true;
    }
    if (flags & GRPC_CHTTP2_DATA_FLAG_END_STREAM) {
      is_closed = true;
      if (type == GRPC_CHTTP2_FRAME_CONTINUATION) {
        gpr_log(GPR_ERROR, "unexpected END_STREAM flag in CONTINUATION frame");
        EXPECT_TRUE(false);
      }
    }
  }
}

static void CrashOnAppendError(absl::string_view, const grpc_core::Slice&) {
  abort();
}

grpc_slice EncodeHeaderIntoBytes(
    bool is_eof,
    const std::vector<std::pair<std::string, std::string>>& header_fields) {
  std::unique_ptr<grpc_core::HPackCompressor> compressor =
      std::make_unique<grpc_core::HPackCompressor>();

  auto arena = grpc_core::MakeScopedArena(1024, g_memory_allocator);
  grpc_metadata_batch b(arena.get());

  for (const auto& field : header_fields) {
    b.Append(field.first,
             grpc_core::Slice::FromStaticString(field.second.c_str()),
             CrashOnAppendError);
  }

  grpc_transport_one_way_stats stats = {};
  grpc_core::HPackCompressor::EncodeHeaderOptions hopt{
      0xdeadbeef, /* stream_id */
      is_eof,     /* is_eof */
      false,      /* use_true_binary_metadata */
      16384,      /* max_frame_size */
      &stats      /* stats */
  };
  grpc_slice_buffer output;
  grpc_slice_buffer_init(&output);

  compressor->EncodeHeaders(hopt, b, &output);
  verify_frames(output, is_eof);

  grpc_slice ret = grpc_slice_merge(output.slices, output.count);
  grpc_slice_buffer_destroy(&output);

  return ret;
}

/* verify that the output generated by encoding the stream matches the
   hexstring passed in */
static void verify(
    bool is_eof, const char* expected,
    const std::vector<std::pair<std::string, std::string>>& header_fields) {
  const grpc_core::Slice merged(EncodeHeaderIntoBytes(is_eof, header_fields));
  const grpc_core::Slice expect(parse_hexstring(expected));

  EXPECT_EQ(merged, expect);
}

TEST(HpackEncoderTest, TestBasicHeaders) {
  grpc_core::ExecCtx exec_ctx;
  g_compressor = new grpc_core::HPackCompressor();

  verify(false, "000005 0104 deadbeef 00 0161 0161", {{"a", "a"}});
  verify(false, "00000a 0104 deadbeef 00 0161 0161 00 0162 0163",
         {{"a", "a"}, {"b", "c"}});

  delete g_compressor;
}

MATCHER(HasLiteralHeaderFieldNewNameFlagIncrementalIndexing, "") {
  constexpr size_t kHttp2FrameHeaderSize = 9u;
  /// Reference: https://httpwg.org/specs/rfc7541.html#rfc.section.6.2.1
  /// The first byte of a literal header field with incremental indexing should
  /// be 0x40.
  constexpr uint8_t kLiteralHeaderFieldNewNameFlagIncrementalIndexing = 0x40;
  return (GRPC_SLICE_START_PTR(arg)[kHttp2FrameHeaderSize] ==
          kLiteralHeaderFieldNewNameFlagIncrementalIndexing);
}

MATCHER(HasLiteralHeaderFieldNewNameFlagNoIndexing, "") {
  constexpr size_t kHttp2FrameHeaderSize = 9u;
  /// Reference: https://httpwg.org/specs/rfc7541.html#rfc.section.6.2.2
  /// The first byte of a literal header field without indexing should be 0x0.
  constexpr uint8_t kLiteralHeaderFieldNewNameFlagNoIndexing = 0x00;
  return (GRPC_SLICE_START_PTR(arg)[kHttp2FrameHeaderSize] ==
          kLiteralHeaderFieldNewNameFlagNoIndexing);
}

TEST(HpackEncoderTest, GrpcTraceBinMetadataIndexing) {
  grpc_core::ExecCtx exec_ctx;

  const grpc_slice encoded_header = EncodeHeaderIntoBytes(
      false, {{grpc_core::GrpcTraceBinMetadata::key().data(), "value"}});
  EXPECT_THAT(encoded_header,
              HasLiteralHeaderFieldNewNameFlagIncrementalIndexing());

  grpc_slice_unref(encoded_header);
}

TEST(HpackEncoderTest, GrpcTraceBinMetadataNoIndexing) {
  grpc_core::ExecCtx exec_ctx;

  /// needs to be greater than `HPackEncoderTable::MaxEntrySize()`
  constexpr size_t long_value_size = 70000u;
  const grpc_slice encoded_header = EncodeHeaderIntoBytes(
      false, {{grpc_core::GrpcTraceBinMetadata::key().data(),
               std::string(long_value_size, 'a')}});
  EXPECT_THAT(encoded_header, HasLiteralHeaderFieldNewNameFlagNoIndexing());

  grpc_slice_unref(encoded_header);
}

TEST(HpackEncoderTest, TestGrpcTagsBinMetadataIndexing) {
  grpc_core::ExecCtx exec_ctx;

  const grpc_slice encoded_header = EncodeHeaderIntoBytes(
      false,
      {{grpc_core::GrpcTagsBinMetadata::key().data(), std::string("value")}});
  EXPECT_THAT(encoded_header,
              HasLiteralHeaderFieldNewNameFlagIncrementalIndexing());

  grpc_slice_unref(encoded_header);
}

TEST(HpackEncoderTest, TestGrpcTagsBinMetadataNoIndexing) {
  grpc_core::ExecCtx exec_ctx;

  /// needs to be greater than `HPackEncoderTable::MaxEntrySize()`
  constexpr size_t long_value_size = 70000u;
  const grpc_slice encoded_header = EncodeHeaderIntoBytes(
      false, {{grpc_core::GrpcTagsBinMetadata::key().data(),
               std::string(long_value_size, 'a')}});
  EXPECT_THAT(encoded_header, HasLiteralHeaderFieldNewNameFlagNoIndexing());

  grpc_slice_unref(encoded_header);
}

TEST(HpackEncoderTest, UserAgentMetadataIndexing) {
  grpc_core::ExecCtx exec_ctx;

  const grpc_slice encoded_header = EncodeHeaderIntoBytes(
      false, {{grpc_core::UserAgentMetadata::key().data(), "value"}});
  EXPECT_THAT(encoded_header,
              HasLiteralHeaderFieldNewNameFlagIncrementalIndexing());

  grpc_slice_unref(encoded_header);
}

TEST(HpackEncoderTest, UserAgentMetadataNoIndexing) {
  grpc_core::ExecCtx exec_ctx;

  /// needs to be greater than `HPackEncoderTable::MaxEntrySize()`
  constexpr size_t long_value_size = 70000u;
  const grpc_slice encoded_header =
      EncodeHeaderIntoBytes(false, {{grpc_core::UserAgentMetadata::key().data(),
                                     std::string(long_value_size, 'a')}});
  EXPECT_THAT(encoded_header, HasLiteralHeaderFieldNewNameFlagNoIndexing());

  grpc_slice_unref(encoded_header);
}

static void verify_continuation_headers(const char* key, const char* value,
                                        bool is_eof) {
  auto arena = grpc_core::MakeScopedArena(1024, g_memory_allocator);
  grpc_slice_buffer output;
  grpc_metadata_batch b(arena.get());
  b.Append(key, grpc_core::Slice::FromStaticString(value), CrashOnAppendError);
  grpc_slice_buffer_init(&output);

  grpc_transport_one_way_stats stats;
  stats = {};
  grpc_core::HPackCompressor::EncodeHeaderOptions hopt = {
      0xdeadbeef, /* stream_id */
      is_eof,     /* is_eof */
      false,      /* use_true_binary_metadata */
      150,        /* max_frame_size */
      &stats /* stats */};
  g_compressor->EncodeHeaders(hopt, b, &output);
  verify_frames(output, is_eof);
  grpc_slice_buffer_destroy(&output);
}

TEST(HpackEncoderTest, TestContinuationHeaders) {
  grpc_core::ExecCtx exec_ctx;
  g_compressor = new grpc_core::HPackCompressor();

  char value[200];
  memset(value, 'a', 200);
  value[199] = 0;  // null terminator
  verify_continuation_headers("key", value, true);

  char value2[400];
  memset(value2, 'b', 400);
  value2[399] = 0;  // null terminator
  verify_continuation_headers("key2", value2, true);

  delete g_compressor;
}

int main(int argc, char** argv) {
  grpc::testing::TestEnvironment env(&argc, argv);
  ::testing::InitGoogleTest(&argc, argv);
  grpc::testing::TestGrpcScope grpc_scope;
  grpc_test_only_set_slice_hash_seed(0);
  return RUN_ALL_TESTS();
}
