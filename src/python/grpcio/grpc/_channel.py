# Copyright 2016 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Invocation-side implementation of gRPC Python."""

import copy
import functools
import logging
import os
import sys
import threading
import time
from abc import abstractmethod
from socket import getaddrinfo as gai
from threading import Event
from typing import List

import grpc
from grpc import _common
from grpc import _compression
from grpc import _grpcio_metadata
from grpc._cython import cygrpc
import grpc.experimental

_LOGGER = logging.getLogger(__name__)

_USER_AGENT = 'grpc-python/{}'.format(_grpcio_metadata.__version__)

_EMPTY_FLAGS = 0

# NOTE(rbellevi): No guarantees are given about the maintenance of this
# environment variable.
_DEFAULT_SINGLE_THREADED_UNARY_STREAM = os.getenv(
    "GRPC_SINGLE_THREADED_UNARY_STREAM") is not None

_UNARY_UNARY_INITIAL_DUE = (
    cygrpc.OperationType.send_initial_metadata,
    cygrpc.OperationType.send_message,
    cygrpc.OperationType.send_close_from_client,
    cygrpc.OperationType.receive_initial_metadata,
    cygrpc.OperationType.receive_message,
    cygrpc.OperationType.receive_status_on_client,
)
_UNARY_STREAM_INITIAL_DUE = (
    cygrpc.OperationType.send_initial_metadata,
    cygrpc.OperationType.send_message,
    cygrpc.OperationType.send_close_from_client,
    cygrpc.OperationType.receive_initial_metadata,
    cygrpc.OperationType.receive_status_on_client,
)
_STREAM_UNARY_INITIAL_DUE = (
    cygrpc.OperationType.send_initial_metadata,
    cygrpc.OperationType.receive_initial_metadata,
    cygrpc.OperationType.receive_message,
    cygrpc.OperationType.receive_status_on_client,
)
_STREAM_STREAM_INITIAL_DUE = (
    cygrpc.OperationType.send_initial_metadata,
    cygrpc.OperationType.receive_initial_metadata,
    cygrpc.OperationType.receive_status_on_client,
)

_CHANNEL_SUBSCRIPTION_CALLBACK_ERROR_LOG_MESSAGE = (
    'Exception calling channel subscription callback!')

_OK_RENDEZVOUS_REPR_FORMAT = ('<{} of RPC that terminated with:\n'
                              '\tstatus = {}\n'
                              '\tdetails = "{}"\n'
                              '>')

_NON_OK_RENDEZVOUS_REPR_FORMAT = ('<{} of RPC that terminated with:\n'
                                  '\tstatus = {}\n'
                                  '\tdetails = "{}"\n'
                                  '\tdebug_error_string = "{}"\n'
                                  '>')


def _deadline(timeout):
    return None if timeout is None else time.time() + timeout


def _unknown_code_details(unknown_cygrpc_code, details):
    return 'Server sent unknown code {} and details "{}"'.format(
        unknown_cygrpc_code, details)


class _RPCState(object):

    def __init__(self, due, initial_metadata, trailing_metadata, code, details):
        # `condition` guards all members of _RPCState. `notify_all` is called on
        # `condition` when the state of the RPC has changed.
        self.condition = threading.Condition()

        # The cygrpc.OperationType objects representing events due from the RPC's
        # completion queue. If an operation is in `due`, it is guaranteed that
        # `operate()` has been called on a corresponding operation. But the
        # converse is not true. That is, in the case of failed `operate()`
        # calls, there may briefly be events in `due` that do not correspond to
        # operations submitted to Core.
        self.due = set(due)
        self.initial_metadata = initial_metadata
        self.response = None
        self.trailing_metadata = trailing_metadata
        self.code = code
        self.details = details
        self.debug_error_string = None

        # The semantics of grpc.Future.cancel and grpc.Future.cancelled are
        # slightly wonky, so they have to be tracked separately from the rest of the
        # result of the RPC. This field tracks whether cancellation was requested
        # prior to termination of the RPC.
        self.cancelled = False
        self.callbacks = []
        self.fork_epoch = cygrpc.get_fork_epoch()

    def reset_postfork_child(self):
        self.condition = threading.Condition()


def _abort(state, code, details):
    if state.code is None:
        state.code = code
        state.details = details
        if state.initial_metadata is None:
            state.initial_metadata = ()
        state.trailing_metadata = ()


def _handle_event(event, state, response_deserializer):
    callbacks = []
    for batch_operation in event.batch_operations:
        operation_type = batch_operation.type()
        state.due.remove(operation_type)
        if operation_type == cygrpc.OperationType.receive_initial_metadata:
            state.initial_metadata = batch_operation.initial_metadata()
        elif operation_type == cygrpc.OperationType.receive_message:
            serialized_response = batch_operation.message()
            if serialized_response is not None:
                response = _common.deserialize(serialized_response,
                                               response_deserializer)
                if response is None:
                    details = 'Exception deserializing response!'
                    _abort(state, grpc.StatusCode.INTERNAL, details)
                else:
                    state.response = response
        elif operation_type == cygrpc.OperationType.receive_status_on_client:
            state.trailing_metadata = batch_operation.trailing_metadata()
            if state.code is None:
                code = _common.CYGRPC_STATUS_CODE_TO_STATUS_CODE.get(
                    batch_operation.code())
                if code is None:
                    state.code = grpc.StatusCode.UNKNOWN
                    state.details = _unknown_code_details(
                        code, batch_operation.details())
                else:
                    state.code = code
                    state.details = batch_operation.details()
                    state.debug_error_string = batch_operation.error_string()
            callbacks.extend(state.callbacks)
            state.callbacks = None
    return callbacks


def _event_handler(state, response_deserializer):

    def handle_event(event):
        with state.condition:
            callbacks = _handle_event(event, state, response_deserializer)
            state.condition.notify_all()
            done = not state.due
        for callback in callbacks:
            try:
                callback()
            except Exception as e:  # pylint: disable=broad-except
                # NOTE(rbellevi): We suppress but log errors here so as not to
                # kill the channel spin thread.
                logging.error('Exception in callback %s: %s',
                              repr(callback.func), repr(e))
        return done and state.fork_epoch >= cygrpc.get_fork_epoch()

    return handle_event


#pylint: disable=too-many-statements
def _consume_request_iterator(request_iterator, state, call, request_serializer,
                              event_handler):
    """Consume a request iterator supplied by the user."""

    def consume_request_iterator():  # pylint: disable=too-many-branches
        # Iterate over the request iterator until it is exhausted or an error
        # condition is encountered.
        while True:
            return_from_user_request_generator_invoked = False
            try:
                # The thread may die in user-code. Do not block fork for this.
                cygrpc.enter_user_request_generator()
                request = next(request_iterator)
            except StopIteration:
                break
            except Exception:  # pylint: disable=broad-except
                cygrpc.return_from_user_request_generator()
                return_from_user_request_generator_invoked = True
                code = grpc.StatusCode.UNKNOWN
                details = 'Exception iterating requests!'
                _LOGGER.exception(details)
                call.cancel(_common.STATUS_CODE_TO_CYGRPC_STATUS_CODE[code],
                            details)
                _abort(state, code, details)
                return
            finally:
                if not return_from_user_request_generator_invoked:
                    cygrpc.return_from_user_request_generator()
            serialized_request = _common.serialize(request, request_serializer)
            with state.condition:
                if state.code is None and not state.cancelled:
                    if serialized_request is None:
                        code = grpc.StatusCode.INTERNAL
                        details = 'Exception serializing request!'
                        call.cancel(
                            _common.STATUS_CODE_TO_CYGRPC_STATUS_CODE[code],
                            details)
                        _abort(state, code, details)
                        return
                    else:
                        state.due.add(cygrpc.OperationType.send_message)
                        operations = (cygrpc.SendMessageOperation(
                            serialized_request, _EMPTY_FLAGS),)
                        operating = call.operate(operations, event_handler)
                        if not operating:
                            state.due.remove(cygrpc.OperationType.send_message)
                            return

                        def _done():
                            return (state.code is not None or
                                    cygrpc.OperationType.send_message
                                    not in state.due)

                        _common.wait(state.condition.wait,
                                     _done,
                                     spin_cb=functools.partial(
                                         cygrpc.block_if_fork_in_progress,
                                         state))
                        if state.code is not None:
                            return
                else:
                    return
        with state.condition:
            if state.code is None:
                state.due.add(cygrpc.OperationType.send_close_from_client)
                operations = (
                    cygrpc.SendCloseFromClientOperation(_EMPTY_FLAGS),)
                operating = call.operate(operations, event_handler)
                if not operating:
                    state.due.remove(
                        cygrpc.OperationType.send_close_from_client)

    consumption_thread = cygrpc.ForkManagedThread(
        target=consume_request_iterator)
    consumption_thread.setDaemon(True)
    consumption_thread.start()


def _rpc_state_string(class_name, rpc_state):
    """Calculates error string for RPC."""
    with rpc_state.condition:
        if rpc_state.code is None:
            return '<{} object>'.format(class_name)
        elif rpc_state.code is grpc.StatusCode.OK:
            return _OK_RENDEZVOUS_REPR_FORMAT.format(class_name, rpc_state.code,
                                                     rpc_state.details)
        else:
            return _NON_OK_RENDEZVOUS_REPR_FORMAT.format(
                class_name, rpc_state.code, rpc_state.details,
                rpc_state.debug_error_string)


class _InactiveRpcError(grpc.RpcError, grpc.Call, grpc.Future):
    """An RPC error not tied to the execution of a particular RPC.

    The RPC represented by the state object must not be in-progress or
    cancelled.

    Attributes:
      _state: An instance of _RPCState.
    """

    def __init__(self, state):
        with state.condition:
            self._state = _RPCState((), copy.deepcopy(state.initial_metadata),
                                    copy.deepcopy(state.trailing_metadata),
                                    state.code, copy.deepcopy(state.details))
            self._state.response = copy.copy(state.response)
            self._state.debug_error_string = copy.copy(state.debug_error_string)

    def initial_metadata(self):
        return self._state.initial_metadata

    def trailing_metadata(self):
        return self._state.trailing_metadata

    def code(self):
        return self._state.code

    def details(self):
        return _common.decode(self._state.details)

    def debug_error_string(self):
        return _common.decode(self._state.debug_error_string)

    def _repr(self):
        return _rpc_state_string(self.__class__.__name__, self._state)

    def __repr__(self):
        return self._repr()

    def __str__(self):
        return self._repr()

    def cancel(self):
        """See grpc.Future.cancel."""
        return False

    def cancelled(self):
        """See grpc.Future.cancelled."""
        return False

    def running(self):
        """See grpc.Future.running."""
        return False

    def done(self):
        """See grpc.Future.done."""
        return True

    def result(self, timeout=None):  # pylint: disable=unused-argument
        """See grpc.Future.result."""
        raise self

    def exception(self, timeout=None):  # pylint: disable=unused-argument
        """See grpc.Future.exception."""
        return self

    def traceback(self, timeout=None):  # pylint: disable=unused-argument
        """See grpc.Future.traceback."""
        try:
            raise self
        except grpc.RpcError:
            return sys.exc_info()[2]

    def add_done_callback(self, fn, timeout=None):  # pylint: disable=unused-argument
        """See grpc.Future.add_done_callback."""
        fn(self)


class _Rendezvous(grpc.RpcError, grpc.RpcContext):
    """An RPC iterator.

    Attributes:
      _state: An instance of _RPCState.
      _call: An instance of SegregatedCall or IntegratedCall.
        In either case, the _call object is expected to have operate, cancel,
        and next_event methods.
      _response_deserializer: A callable taking bytes and return a Python
        object.
      _deadline: A float representing the deadline of the RPC in seconds. Or
        possibly None, to represent an RPC with no deadline at all.
    """

    def __init__(self, state, call, response_deserializer, deadline):
        super(_Rendezvous, self).__init__()
        self._state = state
        self._call = call
        self._response_deserializer = response_deserializer
        self._deadline = deadline

    def is_active(self):
        """See grpc.RpcContext.is_active"""
        with self._state.condition:
            return self._state.code is None

    def time_remaining(self):
        """See grpc.RpcContext.time_remaining"""
        with self._state.condition:
            if self._deadline is None:
                return None
            else:
                return max(self._deadline - time.time(), 0)

    def cancel(self):
        """See grpc.RpcContext.cancel"""
        with self._state.condition:
            if self._state.code is None:
                code = grpc.StatusCode.CANCELLED
                details = 'Locally cancelled by application!'
                self._call.cancel(
                    _common.STATUS_CODE_TO_CYGRPC_STATUS_CODE[code], details)
                self._state.cancelled = True
                _abort(self._state, code, details)
                self._state.condition.notify_all()
                return True
            else:
                return False

    def add_callback(self, callback):
        """See grpc.RpcContext.add_callback"""
        with self._state.condition:
            if self._state.callbacks is None:
                return False
            else:
                self._state.callbacks.append(callback)
                return True

    def __iter__(self):
        return self

    def next(self):
        return self._next()

    def __next__(self):
        return self._next()

    def _next(self):
        raise NotImplementedError()

    def debug_error_string(self):
        raise NotImplementedError()

    def _repr(self):
        return _rpc_state_string(self.__class__.__name__, self._state)

    def __repr__(self):
        return self._repr()

    def __str__(self):
        return self._repr()

    def __del__(self):
        with self._state.condition:
            if self._state.code is None:
                self._state.code = grpc.StatusCode.CANCELLED
                self._state.details = 'Cancelled upon garbage collection!'
                self._state.cancelled = True
                self._call.cancel(
                    _common.STATUS_CODE_TO_CYGRPC_STATUS_CODE[self._state.code],
                    self._state.details)
                self._state.condition.notify_all()


class _SingleThreadedRendezvous(_Rendezvous, grpc.Call, grpc.Future):  # pylint: disable=too-many-ancestors
    """An RPC iterator operating entirely on a single thread.

    The __next__ method of _SingleThreadedRendezvous does not depend on the
    existence of any other thread, including the "channel spin thread".
    However, this means that its interface is entirely synchronous. So this
    class cannot completely fulfill the grpc.Future interface. The result,
    exception, and traceback methods will never block and will instead raise
    an exception if calling the method would result in blocking.

    This means that these methods are safe to call from add_done_callback
    handlers.
    """

    def _is_complete(self):
        return self._state.code is not None

    def cancelled(self):
        with self._state.condition:
            return self._state.cancelled

    def running(self):
        with self._state.condition:
            return self._state.code is None

    def done(self):
        with self._state.condition:
            return self._state.code is not None

    def result(self, timeout=None):
        """Returns the result of the computation or raises its exception.

        This method will never block. Instead, it will raise an exception
        if calling this method would otherwise result in blocking.

        Since this method will never block, any `timeout` argument passed will
        be ignored.
        """
        del timeout
        with self._state.condition:
            if not self._is_complete():
                raise grpc.experimental.UsageError(
                    "_SingleThreadedRendezvous only supports result() when the RPC is complete."
                )
            if self._state.code is grpc.StatusCode.OK:
                return self._state.response
            elif self._state.cancelled:
                raise grpc.FutureCancelledError()
            else:
                raise self

    def exception(self, timeout=None):
        """Return the exception raised by the computation.

        This method will never block. Instead, it will raise an exception
        if calling this method would otherwise result in blocking.

        Since this method will never block, any `timeout` argument passed will
        be ignored.
        """
        del timeout
        with self._state.condition:
            if not self._is_complete():
                raise grpc.experimental.UsageError(
                    "_SingleThreadedRendezvous only supports exception() when the RPC is complete."
                )
            if self._state.code is grpc.StatusCode.OK:
                return None
            elif self._state.cancelled:
                raise grpc.FutureCancelledError()
            else:
                return self

    def traceback(self, timeout=None):
        """Access the traceback of the exception raised by the computation.

        This method will never block. Instead, it will raise an exception
        if calling this method would otherwise result in blocking.

        Since this method will never block, any `timeout` argument passed will
        be ignored.
        """
        del timeout
        with self._state.condition:
            if not self._is_complete():
                raise grpc.experimental.UsageError(
                    "_SingleThreadedRendezvous only supports traceback() when the RPC is complete."
                )
            if self._state.code is grpc.StatusCode.OK:
                return None
            elif self._state.cancelled:
                raise grpc.FutureCancelledError()
            else:
                try:
                    raise self
                except grpc.RpcError:
                    return sys.exc_info()[2]

    def add_done_callback(self, fn):
        with self._state.condition:
            if self._state.code is None:
                self._state.callbacks.append(functools.partial(fn, self))
                return

        fn(self)

    def initial_metadata(self):
        """See grpc.Call.initial_metadata"""
        with self._state.condition:
            # NOTE(gnossen): Based on our initial call batch, we are guaranteed
            # to receive initial metadata before any messages.
            while self._state.initial_metadata is None:
                self._consume_next_event()
            return self._state.initial_metadata

    def trailing_metadata(self):
        """See grpc.Call.trailing_metadata"""
        with self._state.condition:
            if self._state.trailing_metadata is None:
                raise grpc.experimental.UsageError(
                    "Cannot get trailing metadata until RPC is completed.")
            return self._state.trailing_metadata

    def code(self):
        """See grpc.Call.code"""
        with self._state.condition:
            if self._state.code is None:
                raise grpc.experimental.UsageError(
                    "Cannot get code until RPC is completed.")
            return self._state.code

    def details(self):
        """See grpc.Call.details"""
        with self._state.condition:
            if self._state.details is None:
                raise grpc.experimental.UsageError(
                    "Cannot get details until RPC is completed.")
            return _common.decode(self._state.details)

    def _consume_next_event(self):
        event = self._call.next_event()
        with self._state.condition:
            callbacks = _handle_event(event, self._state,
                                      self._response_deserializer)
            for callback in callbacks:
                # NOTE(gnossen): We intentionally allow exceptions to bubble up
                # to the user when running on a single thread.
                callback()
        return event

    def _next_response(self):
        while True:
            self._consume_next_event()
            with self._state.condition:
                if self._state.response is not None:
                    response = self._state.response
                    self._state.response = None
                    return response
                elif cygrpc.OperationType.receive_message not in self._state.due:
                    if self._state.code is grpc.StatusCode.OK:
                        raise StopIteration()
                    elif self._state.code is not None:
                        raise self

    def _next(self):
        with self._state.condition:
            if self._state.code is None:
                # We tentatively add the operation as expected and remove
                # it if the enqueue operation fails. This allows us to guarantee that
                # if an event has been submitted to the core completion queue,
                # it is in `due`. If we waited until after a successful
                # enqueue operation then a signal could interrupt this
                # thread between the enqueue operation and the addition of the
                # operation to `due`. This would cause an exception on the
                # channel spin thread when the operation completes and no
                # corresponding operation would be present in state.due.
                # Note that, since `condition` is held through this block, there is
                # no data race on `due`.
                self._state.due.add(cygrpc.OperationType.receive_message)
                operating = self._call.operate(
                    (cygrpc.ReceiveMessageOperation(_EMPTY_FLAGS),), None)
                if not operating:
                    self._state.due.remove(cygrpc.OperationType.receive_message)
            elif self._state.code is grpc.StatusCode.OK:
                raise StopIteration()
            else:
                raise self
        return self._next_response()

    def debug_error_string(self):
        with self._state.condition:
            if self._state.debug_error_string is None:
                raise grpc.experimental.UsageError(
                    "Cannot get debug error string until RPC is completed.")
            return _common.decode(self._state.debug_error_string)


class _MultiThreadedRendezvous(_Rendezvous, grpc.Call, grpc.Future):  # pylint: disable=too-many-ancestors
    """An RPC iterator that depends on a channel spin thread.

    This iterator relies upon a per-channel thread running in the background,
    dequeueing events from the completion queue, and notifying threads waiting
    on the threading.Condition object in the _RPCState object.

    This extra thread allows _MultiThreadedRendezvous to fulfill the grpc.Future interface
    and to mediate a bidirection streaming RPC.
    """

    def initial_metadata(self):
        """See grpc.Call.initial_metadata"""
        with self._state.condition:

            def _done():
                return self._state.initial_metadata is not None

            _common.wait(self._state.condition.wait, _done)
            return self._state.initial_metadata

    def trailing_metadata(self):
        """See grpc.Call.trailing_metadata"""
        with self._state.condition:

            def _done():
                return self._state.trailing_metadata is not None

            _common.wait(self._state.condition.wait, _done)
            return self._state.trailing_metadata

    def code(self):
        """See grpc.Call.code"""
        with self._state.condition:

            def _done():
                return self._state.code is not None

            _common.wait(self._state.condition.wait, _done)
            return self._state.code

    def details(self):
        """See grpc.Call.details"""
        with self._state.condition:

            def _done():
                return self._state.details is not None

            _common.wait(self._state.condition.wait, _done)
            return _common.decode(self._state.details)

    def debug_error_string(self):
        with self._state.condition:

            def _done():
                return self._state.debug_error_string is not None

            _common.wait(self._state.condition.wait, _done)
            return _common.decode(self._state.debug_error_string)

    def cancelled(self):
        with self._state.condition:
            return self._state.cancelled

    def running(self):
        with self._state.condition:
            return self._state.code is None

    def done(self):
        with self._state.condition:
            return self._state.code is not None

    def _is_complete(self):
        return self._state.code is not None

    def result(self, timeout=None):
        """Returns the result of the computation or raises its exception.

        See grpc.Future.result for the full API contract.
        """
        with self._state.condition:
            timed_out = _common.wait(self._state.condition.wait,
                                     self._is_complete,
                                     timeout=timeout)
            if timed_out:
                raise grpc.FutureTimeoutError()
            else:
                if self._state.code is grpc.StatusCode.OK:
                    return self._state.response
                elif self._state.cancelled:
                    raise grpc.FutureCancelledError()
                else:
                    raise self

    def exception(self, timeout=None):
        """Return the exception raised by the computation.

        See grpc.Future.exception for the full API contract.
        """
        with self._state.condition:
            timed_out = _common.wait(self._state.condition.wait,
                                     self._is_complete,
                                     timeout=timeout)
            if timed_out:
                raise grpc.FutureTimeoutError()
            else:
                if self._state.code is grpc.StatusCode.OK:
                    return None
                elif self._state.cancelled:
                    raise grpc.FutureCancelledError()
                else:
                    return self

    def traceback(self, timeout=None):
        """Access the traceback of the exception raised by the computation.

        See grpc.future.traceback for the full API contract.
        """
        with self._state.condition:
            timed_out = _common.wait(self._state.condition.wait,
                                     self._is_complete,
                                     timeout=timeout)
            if timed_out:
                raise grpc.FutureTimeoutError()
            else:
                if self._state.code is grpc.StatusCode.OK:
                    return None
                elif self._state.cancelled:
                    raise grpc.FutureCancelledError()
                else:
                    try:
                        raise self
                    except grpc.RpcError:
                        return sys.exc_info()[2]

    def add_done_callback(self, fn):
        with self._state.condition:
            if self._state.code is None:
                self._state.callbacks.append(functools.partial(fn, self))
                return

        fn(self)

    def _next(self):
        with self._state.condition:
            if self._state.code is None:
                event_handler = _event_handler(self._state,
                                               self._response_deserializer)
                self._state.due.add(cygrpc.OperationType.receive_message)
                operating = self._call.operate(
                    (cygrpc.ReceiveMessageOperation(_EMPTY_FLAGS),),
                    event_handler)
                if not operating:
                    self._state.due.remove(cygrpc.OperationType.receive_message)
            elif self._state.code is grpc.StatusCode.OK:
                raise StopIteration()
            else:
                raise self

            def _response_ready():
                return (self._state.response is not None or
                        (cygrpc.OperationType.receive_message
                         not in self._state.due and
                         self._state.code is not None))

            _common.wait(self._state.condition.wait, _response_ready)
            if self._state.response is not None:
                response = self._state.response
                self._state.response = None
                return response
            elif cygrpc.OperationType.receive_message not in self._state.due:
                if self._state.code is grpc.StatusCode.OK:
                    raise StopIteration()
                elif self._state.code is not None:
                    raise self


def _start_unary_request(request, timeout, request_serializer):
    deadline = _deadline(timeout)
    serialized_request = _common.serialize(request, request_serializer)
    if serialized_request is None:
        state = _RPCState((), (), (), grpc.StatusCode.INTERNAL,
                          'Exception serializing request!')
        error = _InactiveRpcError(state)
        return deadline, None, error
    else:
        return deadline, serialized_request, None


def _end_unary_response_blocking(state, call, with_call, deadline):
    if state.code is grpc.StatusCode.OK:
        if with_call:
            rendezvous = _MultiThreadedRendezvous(state, call, None, deadline)
            return state.response, rendezvous
        else:
            return state.response
    else:
        raise _InactiveRpcError(state)


def _stream_unary_invocation_operationses(metadata, initial_metadata_flags):
    return (
        (
            cygrpc.SendInitialMetadataOperation(metadata,
                                                initial_metadata_flags),
            cygrpc.ReceiveMessageOperation(_EMPTY_FLAGS),
            cygrpc.ReceiveStatusOnClientOperation(_EMPTY_FLAGS),
        ),
        (cygrpc.ReceiveInitialMetadataOperation(_EMPTY_FLAGS),),
    )


def _stream_unary_invocation_operationses_and_tags(metadata,
                                                   initial_metadata_flags):
    return tuple((
        operations,
        None,
    ) for operations in _stream_unary_invocation_operationses(
        metadata, initial_metadata_flags))


def _determine_deadline(user_deadline):
    parent_deadline = cygrpc.get_deadline_from_context()
    if parent_deadline is None and user_deadline is None:
        return None
    elif parent_deadline is not None and user_deadline is None:
        return parent_deadline
    elif user_deadline is not None and parent_deadline is None:
        return user_deadline
    else:
        return min(parent_deadline, user_deadline)


class _UnaryUnaryMultiCallable(grpc.UnaryUnaryMultiCallable):

    # pylint: disable=too-many-arguments
    def __init__(self, channel, managed_call, method, request_serializer,
                 response_deserializer):
        self._channel = channel
        self._managed_call = managed_call
        self._method = method
        self._request_serializer = request_serializer
        self._response_deserializer = response_deserializer
        self._context = cygrpc.build_census_context()

    def _prepare(self, request, timeout, metadata, wait_for_ready, compression):
        deadline, serialized_request, rendezvous = _start_unary_request(
            request, timeout, self._request_serializer)
        initial_metadata_flags = _InitialMetadataFlags().with_wait_for_ready(
            wait_for_ready)
        augmented_metadata = _compression.augment_metadata(
            metadata, compression)
        if serialized_request is None:
            return None, None, None, rendezvous
        else:
            state = _RPCState(_UNARY_UNARY_INITIAL_DUE, None, None, None, None)
            operations = (
                cygrpc.SendInitialMetadataOperation(augmented_metadata,
                                                    initial_metadata_flags),
                cygrpc.SendMessageOperation(serialized_request, _EMPTY_FLAGS),
                cygrpc.SendCloseFromClientOperation(_EMPTY_FLAGS),
                cygrpc.ReceiveInitialMetadataOperation(_EMPTY_FLAGS),
                cygrpc.ReceiveMessageOperation(_EMPTY_FLAGS),
                cygrpc.ReceiveStatusOnClientOperation(_EMPTY_FLAGS),
            )
            return state, operations, deadline, None

    def _blocking(self, request, timeout, metadata, credentials, wait_for_ready,
                  compression):
        state, operations, deadline, rendezvous = self._prepare(
            request, timeout, metadata, wait_for_ready, compression)
        if state is None:
            raise rendezvous  # pylint: disable-msg=raising-bad-type
        else:
            call = self._channel.segregated_call(
                cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS,
                self._method, None, _determine_deadline(deadline), metadata,
                None if credentials is None else credentials._credentials, ((
                    operations,
                    None,
                ),), self._context)
            event = call.next_event()
            _handle_event(event, state, self._response_deserializer)
            return state, call

    def __call__(self,
                 request,
                 timeout=None,
                 metadata=None,
                 credentials=None,
                 wait_for_ready=None,
                 compression=None):
        state, call, = self._blocking(request, timeout, metadata, credentials,
                                      wait_for_ready, compression)
        return _end_unary_response_blocking(state, call, False, None)

    def with_call(self,
                  request,
                  timeout=None,
                  metadata=None,
                  credentials=None,
                  wait_for_ready=None,
                  compression=None):
        state, call, = self._blocking(request, timeout, metadata, credentials,
                                      wait_for_ready, compression)
        return _end_unary_response_blocking(state, call, True, None)

    def future(self,
               request,
               timeout=None,
               metadata=None,
               credentials=None,
               wait_for_ready=None,
               compression=None):
        state, operations, deadline, rendezvous = self._prepare(
            request, timeout, metadata, wait_for_ready, compression)
        if state is None:
            raise rendezvous  # pylint: disable-msg=raising-bad-type
        else:
            event_handler = _event_handler(state, self._response_deserializer)
            call = self._managed_call(
                cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS,
                self._method, None, deadline, metadata,
                None if credentials is None else credentials._credentials,
                (operations,), event_handler, self._context)
            return _MultiThreadedRendezvous(state, call,
                                            self._response_deserializer,
                                            deadline)


class _SingleThreadedUnaryStreamMultiCallable(grpc.UnaryStreamMultiCallable):

    # pylint: disable=too-many-arguments
    def __init__(self, channel, method, request_serializer,
                 response_deserializer):
        self._channel = channel
        self._method = method
        self._request_serializer = request_serializer
        self._response_deserializer = response_deserializer
        self._context = cygrpc.build_census_context()

    def __call__(  # pylint: disable=too-many-locals
            self,
            request,
            timeout=None,
            metadata=None,
            credentials=None,
            wait_for_ready=None,
            compression=None):
        deadline = _deadline(timeout)
        serialized_request = _common.serialize(request,
                                               self._request_serializer)
        if serialized_request is None:
            state = _RPCState((), (), (), grpc.StatusCode.INTERNAL,
                              'Exception serializing request!')
            raise _InactiveRpcError(state)

        state = _RPCState(_UNARY_STREAM_INITIAL_DUE, None, None, None, None)
        call_credentials = None if credentials is None else credentials._credentials
        initial_metadata_flags = _InitialMetadataFlags().with_wait_for_ready(
            wait_for_ready)
        augmented_metadata = _compression.augment_metadata(
            metadata, compression)
        operations = (
            (cygrpc.SendInitialMetadataOperation(augmented_metadata,
                                                 initial_metadata_flags),
             cygrpc.SendMessageOperation(serialized_request, _EMPTY_FLAGS),
             cygrpc.SendCloseFromClientOperation(_EMPTY_FLAGS)),
            (cygrpc.ReceiveStatusOnClientOperation(_EMPTY_FLAGS),),
            (cygrpc.ReceiveInitialMetadataOperation(_EMPTY_FLAGS),),
        )
        operations_and_tags = tuple((ops, None) for ops in operations)
        call = self._channel.segregated_call(
            cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS, self._method,
            None, _determine_deadline(deadline), metadata, call_credentials,
            operations_and_tags, self._context)
        return _SingleThreadedRendezvous(state, call,
                                         self._response_deserializer, deadline)


class _UnaryStreamMultiCallable(grpc.UnaryStreamMultiCallable):

    # pylint: disable=too-many-arguments
    def __init__(self, channel, managed_call, method, request_serializer,
                 response_deserializer):
        self._channel = channel
        self._managed_call = managed_call
        self._method = method
        self._request_serializer = request_serializer
        self._response_deserializer = response_deserializer
        self._context = cygrpc.build_census_context()

    def __call__(  # pylint: disable=too-many-locals
            self,
            request,
            timeout=None,
            metadata=None,
            credentials=None,
            wait_for_ready=None,
            compression=None):
        deadline, serialized_request, rendezvous = _start_unary_request(
            request, timeout, self._request_serializer)
        initial_metadata_flags = _InitialMetadataFlags().with_wait_for_ready(
            wait_for_ready)
        if serialized_request is None:
            raise rendezvous  # pylint: disable-msg=raising-bad-type
        else:
            augmented_metadata = _compression.augment_metadata(
                metadata, compression)
            state = _RPCState(_UNARY_STREAM_INITIAL_DUE, None, None, None, None)
            operationses = (
                (
                    cygrpc.SendInitialMetadataOperation(augmented_metadata,
                                                        initial_metadata_flags),
                    cygrpc.SendMessageOperation(serialized_request,
                                                _EMPTY_FLAGS),
                    cygrpc.SendCloseFromClientOperation(_EMPTY_FLAGS),
                    cygrpc.ReceiveStatusOnClientOperation(_EMPTY_FLAGS),
                ),
                (cygrpc.ReceiveInitialMetadataOperation(_EMPTY_FLAGS),),
            )
            call = self._managed_call(
                cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS,
                self._method, None, _determine_deadline(deadline), metadata,
                None if credentials is None else credentials._credentials,
                operationses, _event_handler(state,
                                             self._response_deserializer),
                self._context)
            return _MultiThreadedRendezvous(state, call,
                                            self._response_deserializer,
                                            deadline)


class _StreamUnaryMultiCallable(grpc.StreamUnaryMultiCallable):

    # pylint: disable=too-many-arguments
    def __init__(self, channel, managed_call, method, request_serializer,
                 response_deserializer):
        self._channel = channel
        self._managed_call = managed_call
        self._method = method
        self._request_serializer = request_serializer
        self._response_deserializer = response_deserializer
        self._context = cygrpc.build_census_context()

    def _blocking(self, request_iterator, timeout, metadata, credentials,
                  wait_for_ready, compression):
        deadline = _deadline(timeout)
        state = _RPCState(_STREAM_UNARY_INITIAL_DUE, None, None, None, None)
        initial_metadata_flags = _InitialMetadataFlags().with_wait_for_ready(
            wait_for_ready)
        augmented_metadata = _compression.augment_metadata(
            metadata, compression)
        call = self._channel.segregated_call(
            cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS, self._method,
            None, _determine_deadline(deadline), augmented_metadata,
            None if credentials is None else credentials._credentials,
            _stream_unary_invocation_operationses_and_tags(
                augmented_metadata, initial_metadata_flags), self._context)
        _consume_request_iterator(request_iterator, state, call,
                                  self._request_serializer, None)
        while True:
            event = call.next_event()
            with state.condition:
                _handle_event(event, state, self._response_deserializer)
                state.condition.notify_all()
                if not state.due:
                    break
        return state, call

    def __call__(self,
                 request_iterator,
                 timeout=None,
                 metadata=None,
                 credentials=None,
                 wait_for_ready=None,
                 compression=None):
        state, call, = self._blocking(request_iterator, timeout, metadata,
                                      credentials, wait_for_ready, compression)
        return _end_unary_response_blocking(state, call, False, None)

    def with_call(self,
                  request_iterator,
                  timeout=None,
                  metadata=None,
                  credentials=None,
                  wait_for_ready=None,
                  compression=None):
        state, call, = self._blocking(request_iterator, timeout, metadata,
                                      credentials, wait_for_ready, compression)
        return _end_unary_response_blocking(state, call, True, None)

    def future(self,
               request_iterator,
               timeout=None,
               metadata=None,
               credentials=None,
               wait_for_ready=None,
               compression=None):
        deadline = _deadline(timeout)
        state = _RPCState(_STREAM_UNARY_INITIAL_DUE, None, None, None, None)
        event_handler = _event_handler(state, self._response_deserializer)
        initial_metadata_flags = _InitialMetadataFlags().with_wait_for_ready(
            wait_for_ready)
        augmented_metadata = _compression.augment_metadata(
            metadata, compression)
        call = self._managed_call(
            cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS, self._method,
            None, deadline, augmented_metadata,
            None if credentials is None else credentials._credentials,
            _stream_unary_invocation_operationses(metadata,
                                                  initial_metadata_flags),
            event_handler, self._context)
        _consume_request_iterator(request_iterator, state, call,
                                  self._request_serializer, event_handler)
        return _MultiThreadedRendezvous(state, call,
                                        self._response_deserializer, deadline)


class _StreamStreamMultiCallable(grpc.StreamStreamMultiCallable):

    # pylint: disable=too-many-arguments
    def __init__(self, channel, managed_call, method, request_serializer,
                 response_deserializer):
        self._channel = channel
        self._managed_call = managed_call
        self._method = method
        self._request_serializer = request_serializer
        self._response_deserializer = response_deserializer
        self._context = cygrpc.build_census_context()

    def __call__(self,
                 request_iterator,
                 timeout=None,
                 metadata=None,
                 credentials=None,
                 wait_for_ready=None,
                 compression=None):
        deadline = _deadline(timeout)
        state = _RPCState(_STREAM_STREAM_INITIAL_DUE, None, None, None, None)
        initial_metadata_flags = _InitialMetadataFlags().with_wait_for_ready(
            wait_for_ready)
        augmented_metadata = _compression.augment_metadata(
            metadata, compression)
        operationses = (
            (
                cygrpc.SendInitialMetadataOperation(augmented_metadata,
                                                    initial_metadata_flags),
                cygrpc.ReceiveStatusOnClientOperation(_EMPTY_FLAGS),
            ),
            (cygrpc.ReceiveInitialMetadataOperation(_EMPTY_FLAGS),),
        )
        event_handler = _event_handler(state, self._response_deserializer)
        call = self._managed_call(
            cygrpc.PropagationConstants.GRPC_PROPAGATE_DEFAULTS, self._method,
            None, _determine_deadline(deadline), augmented_metadata,
            None if credentials is None else credentials._credentials,
            operationses, event_handler, self._context)
        _consume_request_iterator(request_iterator, state, call,
                                  self._request_serializer, event_handler)
        return _MultiThreadedRendezvous(state, call,
                                        self._response_deserializer, deadline)


class _InitialMetadataFlags(int):
    """Stores immutable initial metadata flags"""

    def __new__(cls, value=_EMPTY_FLAGS):
        value &= cygrpc.InitialMetadataFlags.used_mask
        return super(_InitialMetadataFlags, cls).__new__(cls, value)

    def with_wait_for_ready(self, wait_for_ready):
        if wait_for_ready is not None:
            if wait_for_ready:
                return self.__class__(self | cygrpc.InitialMetadataFlags.wait_for_ready | \
                    cygrpc.InitialMetadataFlags.wait_for_ready_explicitly_set)
            elif not wait_for_ready:
                return self.__class__(self & ~cygrpc.InitialMetadataFlags.wait_for_ready | \
                    cygrpc.InitialMetadataFlags.wait_for_ready_explicitly_set)
        return self


class _ChannelCallState(object):

    def __init__(self, channel):
        self.lock = threading.Lock()
        self.channel = channel
        self.managed_calls = 0
        self.threading = False

    def reset_postfork_child(self):
        self.managed_calls = 0

    def __del__(self):
        try:
            self.channel.close(cygrpc.StatusCode.cancelled,
                               'Channel deallocated!')
        except (TypeError, AttributeError):
            pass


def _run_channel_spin_thread(state):

    def channel_spin():
        while True:
            cygrpc.block_if_fork_in_progress(state)
            event = state.channel.next_call_event()
            if event.completion_type == cygrpc.CompletionType.queue_timeout:
                continue
            call_completed = event.tag(event)
            if call_completed:
                with state.lock:
                    state.managed_calls -= 1
                    if state.managed_calls == 0:
                        return

    channel_spin_thread = cygrpc.ForkManagedThread(target=channel_spin)
    channel_spin_thread.setDaemon(True)
    channel_spin_thread.start()


def _channel_managed_call_management(state):

    # pylint: disable=too-many-arguments
    def create(flags, method, host, deadline, metadata, credentials,
               operationses, event_handler, context):
        """Creates a cygrpc.IntegratedCall.

        Args:
          flags: An integer bitfield of call flags.
          method: The RPC method.
          host: A host string for the created call.
          deadline: A float to be the deadline of the created call or None if
            the call is to have an infinite deadline.
          metadata: The metadata for the call or None.
          credentials: A cygrpc.CallCredentials or None.
          operationses: An iterable of iterables of cygrpc.Operations to be
            started on the call.
          event_handler: A behavior to call to handle the events resultant from
            the operations on the call.
          context: Context object for distributed tracing.
        Returns:
          A cygrpc.IntegratedCall with which to conduct an RPC.
        """
        operationses_and_tags = tuple((
            operations,
            event_handler,
        ) for operations in operationses)
        with state.lock:
            call = state.channel.integrated_call(flags, method, host, deadline,
                                                 metadata, credentials,
                                                 operationses_and_tags, context)
            if state.managed_calls == 0:
                state.managed_calls = 1
                _run_channel_spin_thread(state)
            else:
                state.managed_calls += 1
            return call

    return create


class _ChannelConnectivityState(object):

    def __init__(self, channel):
        self.lock = threading.RLock()
        self.channel = channel
        self.polling = False
        self.connectivity = None
        self.try_to_connect = False
        self.callbacks_and_connectivities = []
        self.delivering = False

    def reset_postfork_child(self):
        self.polling = False
        self.connectivity = None
        self.try_to_connect = False
        self.callbacks_and_connectivities = []
        self.delivering = False


def _deliveries(state):
    callbacks_needing_update = []
    for callback_and_connectivity in state.callbacks_and_connectivities:
        callback, callback_connectivity, = callback_and_connectivity
        if callback_connectivity is not state.connectivity:
            callbacks_needing_update.append(callback)
            callback_and_connectivity[1] = state.connectivity
    return callbacks_needing_update


def _deliver(state, initial_connectivity, initial_callbacks):
    connectivity = initial_connectivity
    callbacks = initial_callbacks
    while True:
        for callback in callbacks:
            cygrpc.block_if_fork_in_progress(state)
            try:
                callback(connectivity)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    _CHANNEL_SUBSCRIPTION_CALLBACK_ERROR_LOG_MESSAGE)
        with state.lock:
            callbacks = _deliveries(state)
            if callbacks:
                connectivity = state.connectivity
            else:
                state.delivering = False
                return


def _spawn_delivery(state, callbacks):
    delivering_thread = cygrpc.ForkManagedThread(target=_deliver,
                                                 args=(
                                                     state,
                                                     state.connectivity,
                                                     callbacks,
                                                 ))
    delivering_thread.setDaemon(True)
    delivering_thread.start()
    state.delivering = True


# NOTE(https://github.com/grpc/grpc/issues/3064): We'd rather not poll.
def _poll_connectivity(state, channel, initial_try_to_connect):
    try_to_connect = initial_try_to_connect
    connectivity = channel.check_connectivity_state(try_to_connect)
    with state.lock:
        state.connectivity = (
            _common.
            CYGRPC_CONNECTIVITY_STATE_TO_CHANNEL_CONNECTIVITY[connectivity])
        callbacks = tuple(
            callback for callback, unused_but_known_to_be_none_connectivity in
            state.callbacks_and_connectivities)
        for callback_and_connectivity in state.callbacks_and_connectivities:
            callback_and_connectivity[1] = state.connectivity
        if callbacks:
            _spawn_delivery(state, callbacks)
    while True:
        event = channel.watch_connectivity_state(connectivity,
                                                 time.time() + 0.2)
        cygrpc.block_if_fork_in_progress(state)
        with state.lock:
            if not state.callbacks_and_connectivities and not state.try_to_connect:
                state.polling = False
                state.connectivity = None
                break
            try_to_connect = state.try_to_connect
            state.try_to_connect = False
        if event.success or try_to_connect:
            connectivity = channel.check_connectivity_state(try_to_connect)
            with state.lock:
                state.connectivity = (
                    _common.CYGRPC_CONNECTIVITY_STATE_TO_CHANNEL_CONNECTIVITY[
                        connectivity])
                if not state.delivering:
                    callbacks = _deliveries(state)
                    if callbacks:
                        _spawn_delivery(state, callbacks)


def _subscribe(state, callback, try_to_connect):
    with state.lock:
        if not state.callbacks_and_connectivities and not state.polling:
            polling_thread = cygrpc.ForkManagedThread(
                target=_poll_connectivity,
                args=(state, state.channel, bool(try_to_connect)))
            polling_thread.setDaemon(True)
            polling_thread.start()
            state.polling = True
            state.callbacks_and_connectivities.append([callback, None])
        elif not state.delivering and state.connectivity is not None:
            _spawn_delivery(state, (callback,))
            state.try_to_connect |= bool(try_to_connect)
            state.callbacks_and_connectivities.append(
                [callback, state.connectivity])
        else:
            state.try_to_connect |= bool(try_to_connect)
            state.callbacks_and_connectivities.append([callback, None])


def _unsubscribe(state, callback):
    with state.lock:
        for index, (subscribed_callback, unused_connectivity) in enumerate(
                state.callbacks_and_connectivities):
            if callback == subscribed_callback:
                state.callbacks_and_connectivities.pop(index)
                break


def _augment_options(base_options, compression):
    compression_option = _compression.create_channel_option(compression)
    return tuple(base_options) + compression_option + ((
        cygrpc.ChannelArgKey.primary_user_agent_string,
        _USER_AGENT,
    ),)


def _separate_channel_options(options):
    """Separates core channel options from Python channel options."""
    core_options = []
    python_options = []
    for pair in options:
        if pair[0] == grpc.experimental.ChannelOptions.SingleThreadedUnaryStream:
            python_options.append(pair)
        else:
            core_options.append(pair)
    return python_options, core_options


class Channel(grpc.Channel):
    """A cygrpc.Channel-backed implementation of grpc.Channel."""

    def __init__(self, target, options, credentials, compression):
        """Constructor.

        Args:
          target: The target to which to connect.
          options: Configuration options for the channel.
          credentials: A cygrpc.ChannelCredentials or None.
          compression: An optional value indicating the compression method to be
            used over the lifetime of the channel.
        """
        python_options, core_options = _separate_channel_options(options)
        self._single_threaded_unary_stream = _DEFAULT_SINGLE_THREADED_UNARY_STREAM
        self._process_python_options(python_options)
        self._channel = cygrpc.Channel(
            _common.encode(target), _augment_options(core_options, compression),
            credentials)
        self._call_state = _ChannelCallState(self._channel)
        self._connectivity_state = _ChannelConnectivityState(self._channel)
        cygrpc.fork_register_channel(self)
        if cygrpc.g_gevent_activated:
            cygrpc.gevent_increment_channel_count()

    def _process_python_options(self, python_options):
        """Sets channel attributes according to python-only channel options."""
        for pair in python_options:
            if pair[0] == grpc.experimental.ChannelOptions.SingleThreadedUnaryStream:
                self._single_threaded_unary_stream = True

    def subscribe(self, callback, try_to_connect=None):
        _subscribe(self._connectivity_state, callback, try_to_connect)

    def unsubscribe(self, callback):
        _unsubscribe(self._connectivity_state, callback)

    def unary_unary(self,
                    method,
                    request_serializer=None,
                    response_deserializer=None):
        return _UnaryUnaryMultiCallable(
            self._channel, _channel_managed_call_management(self._call_state),
            _common.encode(method), request_serializer, response_deserializer)

    def unary_stream(self,
                     method,
                     request_serializer=None,
                     response_deserializer=None):
        # NOTE(rbellevi): Benchmarks have shown that running a unary-stream RPC
        # on a single Python thread results in an appreciable speed-up. However,
        # due to slight differences in capability, the multi-threaded variant
        # remains the default.
        if self._single_threaded_unary_stream:
            return _SingleThreadedUnaryStreamMultiCallable(
                self._channel, _common.encode(method), request_serializer,
                response_deserializer)
        else:
            return _UnaryStreamMultiCallable(
                self._channel,
                _channel_managed_call_management(self._call_state),
                _common.encode(method), request_serializer,
                response_deserializer)

    def stream_unary(self,
                     method,
                     request_serializer=None,
                     response_deserializer=None):
        return _StreamUnaryMultiCallable(
            self._channel, _channel_managed_call_management(self._call_state),
            _common.encode(method), request_serializer, response_deserializer)

    def stream_stream(self,
                      method,
                      request_serializer=None,
                      response_deserializer=None):
        return _StreamStreamMultiCallable(
            self._channel, _channel_managed_call_management(self._call_state),
            _common.encode(method), request_serializer, response_deserializer)

    def _unsubscribe_all(self):
        state = self._connectivity_state
        if state:
            with state.lock:
                del state.callbacks_and_connectivities[:]

    def _close(self):
        self._unsubscribe_all()
        self._channel.close(cygrpc.StatusCode.cancelled, 'Channel closed!')
        cygrpc.fork_unregister_channel(self)
        if cygrpc.g_gevent_activated:
            cygrpc.gevent_decrement_channel_count()

    def _close_on_fork(self):
        self._unsubscribe_all()
        self._channel.close_on_fork(cygrpc.StatusCode.cancelled,
                                    'Channel closed due to fork')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._close()
        return False

    def close(self):
        self._close()

    def __del__(self):
        # TODO(https://github.com/grpc/grpc/issues/12531): Several releases
        # after 1.12 (1.16 or thereabouts?) add a "self._channel.close" call
        # here (or more likely, call self._close() here). We don't do this today
        # because many valid use cases today allow the channel to be deleted
        # immediately after stubs are created. After a sufficient period of time
        # has passed for all users to be trusted to freeze out to their channels
        # for as long as they are in use and to close them after using them,
        # then deletion of this grpc._channel.Channel instance can be made to
        # effect closure of the underlying cygrpc.Channel instance.
        try:
            self._unsubscribe_all()
        except:  # pylint: disable=bare-except
            # Exceptions in __del__ are ignored by Python anyway, but they can
            # keep spamming logs.  Just silence them.
            pass


class ThreadingMixIn(threading.Thread):
    """
    Threading Mix-in class
    """

    def __init__(self):
        super().__init__()
        self._event = Event()
        self._worker_thread = None
        self._await_time = 20

    def thread_start(self, *args, **kwargs):
        """
        thread_start
        :param args:
        :param kwargs:
        :return:
        """
        self._worker_thread = threading.Thread(target=self.thread_run, args=args, kwargs=kwargs)
        self._worker_thread.daemon = True
        self._worker_thread.start()

    def thread_stop(self):
        """
        thread_stop
        :return:
        """
        self._event.set()
        self._worker_thread.join(self._await_time)

    @abstractmethod
    def thread_run(self):
        """

        :return:
        """


class DNSResolver:
    """
    DNSResolver
    """

    @classmethod
    def resolve(cls, host, port):
        """
        resolve
        :param host:
        :param port:
        :return:
        """
        ip_set = set()
        flag = False
        try:
            ip_set = {item[4][0] for item in gai(host, port)}
            flag = True
        except Exception as err:
            print(err)
            print("DNS resolving  failed")
        return flag, ip_set


class ChannelPool:
    """
    ChannelPool
    """

    def __init__(self, host, port, channel_num=1, await_time=20):
        self.host = host
        self.port = port
        self.channel_num = channel_num
        self.await_time = await_time
        self.pool = []
        self.init_channel_pool()

    @abstractmethod
    def init_channel(self, host, port):
        """
        init_channel
        :param host:
        :param port:
        :return:
        """

    def init_channel_pool(self):
        """
        init_channel_pool
        :return:
        """
        self.pool = [self.init_channel(self.host, self.port) for _ in range(self.channel_num)]

    def flush_channel_pool(self):
        """
        flush_channel_pool
        :return:
        """

        def await_close(channel_pool: List[Channel]):
            """

            :param channel_pool:
            :return:
            """
            for channel in channel_pool:
                channel.close()

        tmp_pool = self.pool.copy()
        self.pool = []
        tmp_thread = threading.Thread(target=await_close, args=(tmp_pool,))
        tmp_thread.start()
        tmp_thread.join(self.await_time)

    def refresh_channel_pool(self):
        """

        :return:
        """
        print("refresh")
        self.flush_channel_pool()
        self.init_channel_pool()


class ChannelManager(ThreadingMixIn):
    """
    ChannelManager
    """

    def __init__(self, host="", port="", channel_pool: ChannelPool = None, time_interval=120):
        super().__init__()
        if channel_pool is None and host and port:
            self._channel_pool = ChannelPool(host, port)
        else:
            self._channel_pool = channel_pool
        self.host = self._channel_pool.host
        self.port = self._channel_pool.port
        _, self._ip_set = DNSResolver.resolve(self.host, self.port)
        self.time_interval = time_interval
        self.thread_start()

    @property
    def channel_pool(self):
        """
        channel_pool
        :return:
        """
        return self._channel_pool.pool

    def check_ip_set(self):
        """

        :return:
        """
        flag = False
        dns_flag, ip_set = DNSResolver.resolve(self.host, self.port)
        if dns_flag and ip_set != self._ip_set:
            flag = True
            self._ip_set = ip_set
        return flag

    def thread_run(self):
        """

        :return:
        """
        while True:
            flag = self.check_ip_set()
            print(flag)
            if flag:
                self._channel_pool.refresh_channel_pool()
            time.sleep(self.time_interval)
