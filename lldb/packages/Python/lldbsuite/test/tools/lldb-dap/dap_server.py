#!/usr/bin/env python

import binascii
import json
import optparse
import os
import pprint
import socket
import string
import subprocess
import signal
import sys
import threading
import time
from typing import Any, Optional, Union, BinaryIO, TextIO

## DAP type references
Event = dict[str, Any]
Request = dict[str, Any]
Response = dict[str, Any]
ProtocolMessage = Union[Event, Request, Response]


def dump_memory(base_addr, data, num_per_line, outfile):
    data_len = len(data)
    hex_string = binascii.hexlify(data)
    addr = base_addr
    ascii_str = ""
    i = 0
    while i < data_len:
        outfile.write("0x%8.8x: " % (addr + i))
        bytes_left = data_len - i
        if bytes_left >= num_per_line:
            curr_data_len = num_per_line
        else:
            curr_data_len = bytes_left
        hex_start_idx = i * 2
        hex_end_idx = hex_start_idx + curr_data_len * 2
        curr_hex_str = hex_string[hex_start_idx:hex_end_idx]
        # 'curr_hex_str' now contains the hex byte string for the
        # current line with no spaces between bytes
        t = iter(curr_hex_str)
        # Print hex bytes separated by space
        outfile.write(" ".join(a + b for a, b in zip(t, t)))
        # Print two spaces
        outfile.write("  ")
        # Calculate ASCII string for bytes into 'ascii_str'
        ascii_str = ""
        for j in range(i, i + curr_data_len):
            ch = data[j]
            if ch in string.printable and ch not in string.whitespace:
                ascii_str += "%c" % (ch)
            else:
                ascii_str += "."
        # Print ASCII representation and newline
        outfile.write(ascii_str)
        i = i + curr_data_len
        outfile.write("\n")


def read_packet(f, verbose=False, trace_file=None):
    """Decode a JSON packet that starts with the content length and is
    followed by the JSON bytes from a file 'f'. Returns None on EOF.
    """
    line = f.readline().decode("utf-8")
    if len(line) == 0:
        return None  # EOF.

    # Watch for line that starts with the prefix
    prefix = "Content-Length: "
    if line.startswith(prefix):
        # Decode length of JSON bytes
        if verbose:
            print('content: "%s"' % (line))
        length = int(line[len(prefix) :])
        if verbose:
            print('length: "%u"' % (length))
        # Skip empty line
        line = f.readline()
        if verbose:
            print('empty: "%s"' % (line))
        # Read JSON bytes
        json_str = f.read(length)
        if verbose:
            print('json: "%s"' % (json_str))
        if trace_file:
            trace_file.write("from adapter:\n%s\n" % (json_str))
        # Decode the JSON bytes into a python dictionary
        return json.loads(json_str)

    raise Exception("unexpected malformed message from lldb-dap: " + line)


def packet_type_is(packet, packet_type):
    return "type" in packet and packet["type"] == packet_type


def dump_dap_log(log_file):
    print("========= DEBUG ADAPTER PROTOCOL LOGS =========", file=sys.stderr)
    if log_file is None:
        print("no log file available", file=sys.stderr)
    else:
        with open(log_file, "r") as file:
            print(file.read(), file=sys.stderr)
    print("========= END =========", file=sys.stderr)


class Source(object):
    def __init__(
        self, path: Optional[str] = None, source_reference: Optional[int] = None
    ):
        self._name = None
        self._path = None
        self._source_reference = None

        if path is not None:
            self._name = os.path.basename(path)
            self._path = path
        elif source_reference is not None:
            self._source_reference = source_reference
        else:
            raise ValueError("Either path or source_reference must be provided")

    def __str__(self):
        return f"Source(name={self.name}, path={self.path}), source_reference={self.source_reference})"

    def as_dict(self):
        source_dict = {}
        if self._name is not None:
            source_dict["name"] = self._name
        if self._path is not None:
            source_dict["path"] = self._path
        if self._source_reference is not None:
            source_dict["sourceReference"] = self._source_reference
        return source_dict


class NotSupportedError(KeyError):
    """Raised if a feature is not supported due to its capabilities."""


class DebugCommunication(object):
    def __init__(
        self,
        recv: BinaryIO,
        send: BinaryIO,
        init_commands: list[str],
        log_file: Optional[TextIO] = None,
    ):
        # For debugging test failures, try setting `trace_file = sys.stderr`.
        self.trace_file: Optional[TextIO] = None
        self.log_file = log_file
        self.send = send
        self.recv = recv
        self.recv_packets: list[Optional[ProtocolMessage]] = []
        self.recv_condition = threading.Condition()
        self.recv_thread = threading.Thread(target=self._read_packet_thread)
        self.process_event_body = None
        self.exit_status: Optional[int] = None
        self.capabilities: dict[str, Any] = {}
        self.progress_events: list[Event] = []
        self.reverse_requests = []
        self.sequence = 1
        self.threads = None
        self.thread_stop_reasons = {}
        self.recv_thread.start()
        self.output_condition = threading.Condition()
        self.output: dict[str, list[str]] = {}
        self.configuration_done_sent = False
        self.initialized = False
        self.frame_scopes = {}
        self.init_commands = init_commands
        self.resolved_breakpoints = {}

    @classmethod
    def encode_content(cls, s: str) -> bytes:
        return ("Content-Length: %u\r\n\r\n%s" % (len(s), s)).encode("utf-8")

    @classmethod
    def validate_response(cls, command, response):
        if command["command"] != response["command"]:
            raise ValueError(
                f"command mismatch in response {command['command']} != {response['command']}"
            )
        if command["seq"] != response["request_seq"]:
            raise ValueError(
                f"seq mismatch in response {command['seq']} != {response['request_seq']}"
            )

    def _read_packet_thread(self):
        done = False
        try:
            while not done:
                packet = read_packet(self.recv, trace_file=self.trace_file)
                # `packet` will be `None` on EOF. We want to pass it down to
                # handle_recv_packet anyway so the main thread can handle unexpected
                # termination of lldb-dap and stop waiting for new packets.
                done = not self._handle_recv_packet(packet)
        finally:
            dump_dap_log(self.log_file)

    def get_modules(self, startModule: int = 0, moduleCount: int = 0):
        module_list = self.request_modules(startModule, moduleCount)["body"]["modules"]
        modules = {}
        for module in module_list:
            modules[module["name"]] = module
        return modules

    def get_output(self, category, timeout=0.0, clear=True):
        self.output_condition.acquire()
        output = None
        if category in self.output:
            output = self.output[category]
            if clear:
                del self.output[category]
        elif timeout != 0.0:
            self.output_condition.wait(timeout)
            if category in self.output:
                output = self.output[category]
                if clear:
                    del self.output[category]
        self.output_condition.release()
        return output

    def collect_output(self, category, timeout_secs, pattern, clear=True):
        end_time = time.time() + timeout_secs
        collected_output = ""
        while end_time > time.time():
            output = self.get_output(category, timeout=0.25, clear=clear)
            if output:
                collected_output += output
                if pattern is not None and pattern in output:
                    break
        return collected_output if collected_output else None

    def _enqueue_recv_packet(self, packet: Optional[ProtocolMessage]):
        self.recv_condition.acquire()
        self.recv_packets.append(packet)
        self.recv_condition.notify()
        self.recv_condition.release()

    def _handle_recv_packet(self, packet: Optional[ProtocolMessage]) -> bool:
        """Called by the read thread that is waiting for all incoming packets
        to store the incoming packet in "self.recv_packets" in a thread safe
        way. This function will then signal the "self.recv_condition" to
        indicate a new packet is available. Returns True if the caller
        should keep calling this function for more packets.
        """
        # If EOF, notify the read thread by enqueuing a None.
        if not packet:
            self._enqueue_recv_packet(None)
            return False

        # Check the packet to see if is an event packet
        keepGoing = True
        packet_type = packet["type"]
        if packet_type == "event":
            event = packet["event"]
            body = None
            if "body" in packet:
                body = packet["body"]
            # Handle the event packet and cache information from these packets
            # as they come in
            if event == "output":
                # Store any output we receive so clients can retrieve it later.
                category = body["category"]
                output = body["output"]
                self.output_condition.acquire()
                if category in self.output:
                    self.output[category] += output
                else:
                    self.output[category] = output
                self.output_condition.notify()
                self.output_condition.release()
                # no need to add 'output' event packets to our packets list
                return keepGoing
            elif event == "initialized":
                self.initialized = True
            elif event == "process":
                # When a new process is attached or launched, remember the
                # details that are available in the body of the event
                self.process_event_body = body
            elif event == "exited":
                # Process exited, mark the status to indicate the process is not
                # alive.
                self.exit_status = body["exitCode"]
            elif event == "continued":
                # When the process continues, clear the known threads and
                # thread_stop_reasons.
                all_threads_continued = body.get("allThreadsContinued", True)
                tid = body["threadId"]
                if tid in self.thread_stop_reasons:
                    del self.thread_stop_reasons[tid]
                self._process_continued(all_threads_continued)
            elif event == "stopped":
                # Each thread that stops with a reason will send a
                # 'stopped' event. We need to remember the thread stop
                # reasons since the 'threads' command doesn't return
                # that information.
                self._process_stopped()
                tid = body["threadId"]
                self.thread_stop_reasons[tid] = body
            elif event.startswith("progress"):
                # Progress events come in as 'progressStart', 'progressUpdate',
                # and 'progressEnd' events. Keep these around in case test
                # cases want to verify them.
                self.progress_events.append(packet)
            elif event == "breakpoint":
                # Breakpoint events are sent when a breakpoint is resolved
                self._update_verified_breakpoints([body["breakpoint"]])
            elif event == "capabilities":
                # Update the capabilities with new ones from the event.
                self.capabilities.update(body["capabilities"])

        elif packet_type == "response":
            if packet["command"] == "disconnect":
                keepGoing = False
        self._enqueue_recv_packet(packet)
        return keepGoing

    def _process_continued(self, all_threads_continued: bool):
        self.frame_scopes = {}
        if all_threads_continued:
            self.thread_stop_reasons = {}

    def _update_verified_breakpoints(self, breakpoints: list[Event]):
        for breakpoint in breakpoints:
            if "id" in breakpoint:
                self.resolved_breakpoints[str(breakpoint["id"])] = breakpoint.get(
                    "verified", False
                )

    def send_packet(self, command_dict: Request, set_sequence=True):
        """Take the "command_dict" python dictionary and encode it as a JSON
        string and send the contents as a packet to the VSCode debug
        adapter"""
        # Set the sequence ID for this command automatically
        if set_sequence:
            command_dict["seq"] = self.sequence
            self.sequence += 1
        # Encode our command dictionary as a JSON string
        json_str = json.dumps(command_dict, separators=(",", ":"))
        if self.trace_file:
            self.trace_file.write("to adapter:\n%s\n" % (json_str))
        length = len(json_str)
        if length > 0:
            # Send the encoded JSON packet and flush the 'send' file
            self.send.write(self.encode_content(json_str))
            self.send.flush()

    def recv_packet(
        self,
        filter_type: Optional[str] = None,
        filter_event: Optional[Union[str, list[str]]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[ProtocolMessage]:
        """Get a JSON packet from the VSCode debug adapter. This function
        assumes a thread that reads packets is running and will deliver
        any received packets by calling handle_recv_packet(...). This
        function will wait for the packet to arrive and return it when
        it does."""
        while True:
            try:
                self.recv_condition.acquire()
                packet = None
                while True:
                    for i, curr_packet in enumerate(self.recv_packets):
                        if not curr_packet:
                            raise EOFError
                        packet_type = curr_packet["type"]
                        if filter_type is None or packet_type in filter_type:
                            if filter_event is None or (
                                packet_type == "event"
                                and curr_packet["event"] in filter_event
                            ):
                                packet = self.recv_packets.pop(i)
                                break
                    if packet:
                        break
                    # Sleep until packet is received
                    len_before = len(self.recv_packets)
                    self.recv_condition.wait(timeout)
                    len_after = len(self.recv_packets)
                    if len_before == len_after:
                        return None  # Timed out
                return packet
            except EOFError:
                return None
            finally:
                self.recv_condition.release()

    def send_recv(self, command):
        """Send a command python dictionary as JSON and receive the JSON
        response. Validates that the response is the correct sequence and
        command in the reply. Any events that are received are added to the
        events list in this object"""
        self.send_packet(command)
        done = False
        while not done:
            response_or_request = self.recv_packet(filter_type=["response", "request"])
            if response_or_request is None:
                desc = 'no response for "%s"' % (command["command"])
                raise ValueError(desc)
            if response_or_request["type"] == "response":
                self.validate_response(command, response_or_request)
                return response_or_request
            else:
                self.reverse_requests.append(response_or_request)
                if response_or_request["command"] == "runInTerminal":
                    subprocess.Popen(
                        response_or_request["arguments"].get("args"),
                        env=response_or_request["arguments"].get("env", {}),
                    )
                    self.send_packet(
                        {
                            "type": "response",
                            "request_seq": response_or_request["seq"],
                            "success": True,
                            "command": "runInTerminal",
                            "body": {},
                        },
                    )
                elif response_or_request["command"] == "startDebugging":
                    self.send_packet(
                        {
                            "type": "response",
                            "request_seq": response_or_request["seq"],
                            "success": True,
                            "command": "startDebugging",
                            "body": {},
                        },
                    )
                else:
                    desc = 'unknown reverse request "%s"' % (
                        response_or_request["command"]
                    )
                    raise ValueError(desc)

        return None

    def wait_for_event(
        self, filter: Union[str, list[str]], timeout: Optional[float] = None
    ) -> Optional[Event]:
        """Wait for the first event that matches the filter."""
        return self.recv_packet(
            filter_type="event", filter_event=filter, timeout=timeout
        )

    def wait_for_stopped(
        self, timeout: Optional[float] = None
    ) -> Optional[list[Event]]:
        stopped_events = []
        stopped_event = self.wait_for_event(
            filter=["stopped", "exited"], timeout=timeout
        )
        while stopped_event:
            stopped_events.append(stopped_event)
            # If we exited, then we are done
            if stopped_event["event"] == "exited":
                break
            # Otherwise we stopped and there might be one or more 'stopped'
            # events for each thread that stopped with a reason, so keep
            # checking for more 'stopped' events and return all of them
            stopped_event = self.wait_for_event(
                filter=["stopped", "exited"], timeout=0.25
            )
        return stopped_events

    def wait_for_breakpoint_events(self, timeout: Optional[float] = None):
        breakpoint_events: list[Event] = []
        while True:
            event = self.wait_for_event("breakpoint", timeout=timeout)
            if not event:
                break
            breakpoint_events.append(event)
        return breakpoint_events

    def wait_for_breakpoints_to_be_verified(
        self, breakpoint_ids: list[str], timeout: Optional[float] = None
    ):
        """Wait for all breakpoints to be verified. Return all unverified breakpoints."""
        while any(id not in self.resolved_breakpoints for id in breakpoint_ids):
            breakpoint_event = self.wait_for_event("breakpoint", timeout=timeout)
            if breakpoint_event is None:
                break

        return [id for id in breakpoint_ids if id not in self.resolved_breakpoints]

    def wait_for_exited(self, timeout: Optional[float] = None):
        event_dict = self.wait_for_event("exited", timeout=timeout)
        if event_dict is None:
            raise ValueError("didn't get exited event")
        return event_dict

    def wait_for_terminated(self, timeout: Optional[float] = None):
        event_dict = self.wait_for_event("terminated", timeout)
        if event_dict is None:
            raise ValueError("didn't get terminated event")
        return event_dict

    def get_capability(self, key: str):
        """Get a value for the given key if it there is a key/value pair in
        the capabilities reported by the adapter.
        """
        if key in self.capabilities:
            return self.capabilities[key]
        raise NotSupportedError(key)

    def get_threads(self):
        if self.threads is None:
            self.request_threads()
        return self.threads

    def get_thread_id(self, threadIndex=0):
        """Utility function to get the first thread ID in the thread list.
        If the thread list is empty, then fetch the threads.
        """
        if self.threads is None:
            self.request_threads()
        if self.threads and threadIndex < len(self.threads):
            return self.threads[threadIndex]["id"]
        return None

    def get_stackFrame(self, frameIndex=0, threadId=None):
        """Get a single "StackFrame" object from a "stackTrace" request and
        return the "StackFrame" as a python dictionary, or None on failure
        """
        if threadId is None:
            threadId = self.get_thread_id()
        if threadId is None:
            print("invalid threadId")
            return None
        response = self.request_stackTrace(threadId, startFrame=frameIndex, levels=1)
        if response:
            return response["body"]["stackFrames"][0]
        print("invalid response")
        return None

    def get_completions(self, text, frameId=None):
        if frameId is None:
            stackFrame = self.get_stackFrame()
            frameId = stackFrame["id"]
        response = self.request_completions(text, frameId)
        return response["body"]["targets"]

    def get_scope_variables(self, scope_name, frameIndex=0, threadId=None, is_hex=None):
        stackFrame = self.get_stackFrame(frameIndex=frameIndex, threadId=threadId)
        if stackFrame is None:
            return []
        frameId = stackFrame["id"]
        if frameId in self.frame_scopes:
            frame_scopes = self.frame_scopes[frameId]
        else:
            scopes_response = self.request_scopes(frameId)
            frame_scopes = scopes_response["body"]["scopes"]
            self.frame_scopes[frameId] = frame_scopes
        for scope in frame_scopes:
            if scope["name"] == scope_name:
                varRef = scope["variablesReference"]
                variables_response = self.request_variables(varRef, is_hex=is_hex)
                if variables_response:
                    if "body" in variables_response:
                        body = variables_response["body"]
                        if "variables" in body:
                            vars = body["variables"]
                            return vars
        return []

    def get_global_variables(self, frameIndex=0, threadId=None):
        return self.get_scope_variables(
            "Globals", frameIndex=frameIndex, threadId=threadId
        )

    def get_local_variables(self, frameIndex=0, threadId=None, is_hex=None):
        return self.get_scope_variables(
            "Locals", frameIndex=frameIndex, threadId=threadId, is_hex=is_hex
        )

    def get_registers(self, frameIndex=0, threadId=None):
        return self.get_scope_variables(
            "Registers", frameIndex=frameIndex, threadId=threadId
        )

    def get_local_variable(self, name, frameIndex=0, threadId=None, is_hex=None):
        locals = self.get_local_variables(
            frameIndex=frameIndex, threadId=threadId, is_hex=is_hex
        )
        for local in locals:
            if "name" in local and local["name"] == name:
                return local
        return None

    def get_local_variable_value(self, name, frameIndex=0, threadId=None, is_hex=None):
        variable = self.get_local_variable(
            name, frameIndex=frameIndex, threadId=threadId, is_hex=is_hex
        )
        if variable and "value" in variable:
            return variable["value"]
        return None

    def get_local_variable_child(
        self, name, child_name, frameIndex=0, threadId=None, is_hex=None
    ):
        local = self.get_local_variable(name, frameIndex, threadId)
        if local["variablesReference"] == 0:
            return None
        children = self.request_variables(local["variablesReference"], is_hex=is_hex)[
            "body"
        ]["variables"]
        for child in children:
            if child["name"] == child_name:
                return child
        return None

    def replay_packets(self, replay_file_path):
        f = open(replay_file_path, "r")
        mode = "invalid"
        set_sequence = False
        command_dict = None
        while mode != "eof":
            if mode == "invalid":
                line = f.readline()
                if line.startswith("to adapter:"):
                    mode = "send"
                elif line.startswith("from adapter:"):
                    mode = "recv"
            elif mode == "send":
                command_dict = read_packet(f)
                # Skip the end of line that follows the JSON
                f.readline()
                if command_dict is None:
                    raise ValueError("decode packet failed from replay file")
                print("Sending:")
                pprint.PrettyPrinter(indent=2).pprint(command_dict)
                # raw_input('Press ENTER to send:')
                self.send_packet(command_dict, set_sequence)
                mode = "invalid"
            elif mode == "recv":
                print("Replay response:")
                replay_response = read_packet(f)
                # Skip the end of line that follows the JSON
                f.readline()
                pprint.PrettyPrinter(indent=2).pprint(replay_response)
                actual_response = self.recv_packet()
                if actual_response:
                    type = actual_response["type"]
                    print("Actual response:")
                    if type == "response":
                        self.validate_response(command_dict, actual_response)
                    pprint.PrettyPrinter(indent=2).pprint(actual_response)
                else:
                    print("error: didn't get a valid response")
                mode = "invalid"

    def request_attach(
        self,
        *,
        program: Optional[str] = None,
        pid: Optional[int] = None,
        waitFor=False,
        initCommands: Optional[list[str]] = None,
        preRunCommands: Optional[list[str]] = None,
        attachCommands: Optional[list[str]] = None,
        postRunCommands: Optional[list[str]] = None,
        stopCommands: Optional[list[str]] = None,
        exitCommands: Optional[list[str]] = None,
        terminateCommands: Optional[list[str]] = None,
        coreFile: Optional[str] = None,
        stopOnEntry=False,
        sourceMap: Optional[Union[list[tuple[str, str]], dict[str, str]]] = None,
        gdbRemotePort: Optional[int] = None,
        gdbRemoteHostname: Optional[str] = None,
    ):
        args_dict = {}
        if pid is not None:
            args_dict["pid"] = pid
        if program is not None:
            args_dict["program"] = program
        if waitFor:
            args_dict["waitFor"] = waitFor
        args_dict["initCommands"] = self.init_commands
        if initCommands:
            args_dict["initCommands"].extend(initCommands)
        if preRunCommands:
            args_dict["preRunCommands"] = preRunCommands
        if stopCommands:
            args_dict["stopCommands"] = stopCommands
        if exitCommands:
            args_dict["exitCommands"] = exitCommands
        if terminateCommands:
            args_dict["terminateCommands"] = terminateCommands
        if attachCommands:
            args_dict["attachCommands"] = attachCommands
        if coreFile:
            args_dict["coreFile"] = coreFile
        if stopOnEntry:
            args_dict["stopOnEntry"] = stopOnEntry
        if postRunCommands:
            args_dict["postRunCommands"] = postRunCommands
        if sourceMap:
            args_dict["sourceMap"] = sourceMap
        if gdbRemotePort is not None:
            args_dict["gdb-remote-port"] = gdbRemotePort
        if gdbRemoteHostname is not None:
            args_dict["gdb-remote-hostname"] = gdbRemoteHostname
        command_dict = {"command": "attach", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_breakpointLocations(
        self, file_path, line, end_line=None, column=None, end_column=None
    ):
        (dir, base) = os.path.split(file_path)
        source_dict = {"name": base, "path": file_path}
        args_dict = {}
        args_dict["source"] = source_dict
        if line is not None:
            args_dict["line"] = line
        if end_line is not None:
            args_dict["endLine"] = end_line
        if column is not None:
            args_dict["column"] = column
        if end_column is not None:
            args_dict["endColumn"] = end_column
        command_dict = {
            "command": "breakpointLocations",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_configurationDone(self):
        command_dict = {
            "command": "configurationDone",
            "type": "request",
            "arguments": {},
        }
        response = self.send_recv(command_dict)
        if response:
            self.configuration_done_sent = True
            self.request_threads()
        return response

    def _process_stopped(self):
        self.threads = None
        self.frame_scopes = {}

    def request_continue(self, threadId=None, singleThread=False):
        if self.exit_status is not None:
            raise ValueError("request_continue called after process exited")
        # If we have launched or attached, then the first continue is done by
        # sending the 'configurationDone' request
        if not self.configuration_done_sent:
            return self.request_configurationDone()
        args_dict = {}
        if threadId is None:
            threadId = self.get_thread_id()
        if threadId:
            args_dict["threadId"] = threadId
        if singleThread:
            args_dict["singleThread"] = True
        command_dict = {
            "command": "continue",
            "type": "request",
            "arguments": args_dict,
        }
        response = self.send_recv(command_dict)
        if response["success"]:
            self._process_continued(response["body"]["allThreadsContinued"])
        # Caller must still call wait_for_stopped.
        return response

    def request_restart(self, restartArguments=None):
        if self.exit_status is not None:
            raise ValueError("request_restart called after process exited")
        self.get_capability("supportsRestartRequest")
        command_dict = {
            "command": "restart",
            "type": "request",
        }
        if restartArguments:
            command_dict["arguments"] = restartArguments

        response = self.send_recv(command_dict)
        # Caller must still call wait_for_stopped.
        return response

    def request_disconnect(self, terminateDebuggee=None):
        args_dict = {}
        if terminateDebuggee is not None:
            if terminateDebuggee:
                args_dict["terminateDebuggee"] = True
            else:
                args_dict["terminateDebuggee"] = False
        command_dict = {
            "command": "disconnect",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_disassemble(
        self,
        memoryReference,
        instructionOffset=-50,
        instructionCount=200,
        resolveSymbols=True,
    ):
        args_dict = {
            "memoryReference": memoryReference,
            "instructionOffset": instructionOffset,
            "instructionCount": instructionCount,
            "resolveSymbols": resolveSymbols,
        }
        command_dict = {
            "command": "disassemble",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)["body"]["instructions"]

    def request_readMemory(self, memoryReference, offset, count):
        args_dict = {
            "memoryReference": memoryReference,
            "offset": offset,
            "count": count,
        }
        command_dict = {
            "command": "readMemory",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_writeMemory(self, memoryReference, data, offset=0, allowPartial=False):
        args_dict = {
            "memoryReference": memoryReference,
            "data": data,
        }

        if offset:
            args_dict["offset"] = offset
        if allowPartial:
            args_dict["allowPartial"] = allowPartial

        command_dict = {
            "command": "writeMemory",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_evaluate(self, expression, frameIndex=0, threadId=None, context=None):
        stackFrame = self.get_stackFrame(frameIndex=frameIndex, threadId=threadId)
        if stackFrame is None:
            return []
        args_dict = {
            "expression": expression,
            "context": context,
            "frameId": stackFrame["id"],
        }
        command_dict = {
            "command": "evaluate",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_exceptionInfo(self, threadId=None):
        if threadId is None:
            threadId = self.get_thread_id()
        args_dict = {"threadId": threadId}
        command_dict = {
            "command": "exceptionInfo",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_initialize(self, sourceInitFile=False):
        command_dict = {
            "command": "initialize",
            "type": "request",
            "arguments": {
                "adapterID": "lldb-native",
                "clientID": "vscode",
                "columnsStartAt1": True,
                "linesStartAt1": True,
                "locale": "en-us",
                "pathFormat": "path",
                "supportsRunInTerminalRequest": True,
                "supportsVariablePaging": True,
                "supportsVariableType": True,
                "supportsStartDebuggingRequest": True,
                "supportsProgressReporting": True,
                "$__lldb_sourceInitFile": sourceInitFile,
            },
        }
        response = self.send_recv(command_dict)
        if response:
            if "body" in response:
                self.capabilities = response["body"]
        return response

    def request_launch(
        self,
        program: str,
        *,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stopOnEntry=False,
        disableASLR=False,
        disableSTDIO=False,
        shellExpandArguments=False,
        console: Optional[str] = None,
        enableAutoVariableSummaries=False,
        displayExtendedBacktrace=False,
        enableSyntheticChildDebugging=False,
        initCommands: Optional[list[str]] = None,
        preRunCommands: Optional[list[str]] = None,
        launchCommands: Optional[list[str]] = None,
        postRunCommands: Optional[list[str]] = None,
        stopCommands: Optional[list[str]] = None,
        exitCommands: Optional[list[str]] = None,
        terminateCommands: Optional[list[str]] = None,
        sourceMap: Optional[Union[list[tuple[str, str]], dict[str, str]]] = None,
        sourcePath: Optional[str] = None,
        debuggerRoot: Optional[str] = None,
        commandEscapePrefix: Optional[str] = None,
        customFrameFormat: Optional[str] = None,
        customThreadFormat: Optional[str] = None,
    ):
        args_dict = {"program": program}
        if args:
            args_dict["args"] = args
        if cwd:
            args_dict["cwd"] = cwd
        if env:
            args_dict["env"] = env
        if stopOnEntry:
            args_dict["stopOnEntry"] = stopOnEntry
        if disableSTDIO:
            args_dict["disableSTDIO"] = disableSTDIO
        if shellExpandArguments:
            args_dict["shellExpandArguments"] = shellExpandArguments
        args_dict["initCommands"] = self.init_commands
        if initCommands:
            args_dict["initCommands"].extend(initCommands)
        if preRunCommands:
            args_dict["preRunCommands"] = preRunCommands
        if stopCommands:
            args_dict["stopCommands"] = stopCommands
        if exitCommands:
            args_dict["exitCommands"] = exitCommands
        if terminateCommands:
            args_dict["terminateCommands"] = terminateCommands
        if sourcePath:
            args_dict["sourcePath"] = sourcePath
        if debuggerRoot:
            args_dict["debuggerRoot"] = debuggerRoot
        if launchCommands:
            args_dict["launchCommands"] = launchCommands
        if sourceMap:
            args_dict["sourceMap"] = sourceMap
        if console:
            args_dict["console"] = console
        if postRunCommands:
            args_dict["postRunCommands"] = postRunCommands
        if customFrameFormat:
            args_dict["customFrameFormat"] = customFrameFormat
        if customThreadFormat:
            args_dict["customThreadFormat"] = customThreadFormat

        args_dict["disableASLR"] = disableASLR
        args_dict["enableAutoVariableSummaries"] = enableAutoVariableSummaries
        args_dict["enableSyntheticChildDebugging"] = enableSyntheticChildDebugging
        args_dict["displayExtendedBacktrace"] = displayExtendedBacktrace
        if commandEscapePrefix is not None:
            args_dict["commandEscapePrefix"] = commandEscapePrefix
        command_dict = {"command": "launch", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_next(self, threadId, granularity="statement"):
        if self.exit_status is not None:
            raise ValueError("request_continue called after process exited")
        args_dict = {"threadId": threadId, "granularity": granularity}
        command_dict = {"command": "next", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_stepIn(self, threadId, targetId, granularity="statement"):
        if self.exit_status is not None:
            raise ValueError("request_stepIn called after process exited")
        if threadId is None:
            threadId = self.get_thread_id()
        args_dict = {
            "threadId": threadId,
            "targetId": targetId,
            "granularity": granularity,
        }
        command_dict = {"command": "stepIn", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_stepInTargets(self, frameId):
        if self.exit_status is not None:
            raise ValueError("request_stepInTargets called after process exited")
        self.get_capability("supportsStepInTargetsRequest")
        args_dict = {"frameId": frameId}
        command_dict = {
            "command": "stepInTargets",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_stepOut(self, threadId):
        if self.exit_status is not None:
            raise ValueError("request_stepOut called after process exited")
        args_dict = {"threadId": threadId}
        command_dict = {"command": "stepOut", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_pause(self, threadId=None):
        if self.exit_status is not None:
            raise ValueError("request_pause called after process exited")
        if threadId is None:
            threadId = self.get_thread_id()
        args_dict = {"threadId": threadId}
        command_dict = {"command": "pause", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_scopes(self, frameId):
        args_dict = {"frameId": frameId}
        command_dict = {"command": "scopes", "type": "request", "arguments": args_dict}
        return self.send_recv(command_dict)

    def request_setBreakpoints(self, source: Source, line_array, data=None):
        """data is array of parameters for breakpoints in line_array.
        Each parameter object is 1:1 mapping with entries in line_entry.
        It contains optional location/hitCondition/logMessage parameters.
        """
        args_dict = {
            "source": source.as_dict(),
            "sourceModified": False,
        }
        if line_array is not None:
            args_dict["lines"] = line_array
            breakpoints = []
            for i, line in enumerate(line_array):
                breakpoint_data = None
                if data is not None and i < len(data):
                    breakpoint_data = data[i]
                bp = {"line": line}
                if breakpoint_data is not None:
                    if breakpoint_data.get("condition"):
                        bp["condition"] = breakpoint_data["condition"]
                    if breakpoint_data.get("hitCondition"):
                        bp["hitCondition"] = breakpoint_data["hitCondition"]
                    if breakpoint_data.get("logMessage"):
                        bp["logMessage"] = breakpoint_data["logMessage"]
                    if breakpoint_data.get("column"):
                        bp["column"] = breakpoint_data["column"]
                breakpoints.append(bp)
            args_dict["breakpoints"] = breakpoints

        command_dict = {
            "command": "setBreakpoints",
            "type": "request",
            "arguments": args_dict,
        }
        response = self.send_recv(command_dict)
        if response["success"]:
            self._update_verified_breakpoints(response["body"]["breakpoints"])
        return response

    def request_setExceptionBreakpoints(
        self, *, filters: list[str] = [], filter_options: list[dict] = []
    ):
        args_dict = {"filters": filters}
        if filter_options:
            args_dict["filterOptions"] = filter_options
        command_dict = {
            "command": "setExceptionBreakpoints",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_setFunctionBreakpoints(self, names, condition=None, hitCondition=None):
        breakpoints = []
        for name in names:
            bp = {"name": name}
            if condition is not None:
                bp["condition"] = condition
            if hitCondition is not None:
                bp["hitCondition"] = hitCondition
            breakpoints.append(bp)
        args_dict = {"breakpoints": breakpoints}
        command_dict = {
            "command": "setFunctionBreakpoints",
            "type": "request",
            "arguments": args_dict,
        }
        response = self.send_recv(command_dict)
        if response["success"]:
            self._update_verified_breakpoints(response["body"]["breakpoints"])
        return response

    def request_dataBreakpointInfo(
        self, variablesReference, name, frameIndex=0, threadId=None
    ):
        stackFrame = self.get_stackFrame(frameIndex=frameIndex, threadId=threadId)
        if stackFrame is None:
            return []
        args_dict = {
            "variablesReference": variablesReference,
            "name": name,
            "frameId": stackFrame["id"],
        }
        command_dict = {
            "command": "dataBreakpointInfo",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_setDataBreakpoint(self, dataBreakpoints):
        """dataBreakpoints is a list of dictionary with following fields:
        {
            dataId: (address in hex)/(size in bytes)
            accessType: read/write/readWrite
            [condition]: string
            [hitCondition]: string
        }
        """
        args_dict = {"breakpoints": dataBreakpoints}
        command_dict = {
            "command": "setDataBreakpoints",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_compileUnits(self, moduleId):
        args_dict = {"moduleId": moduleId}
        command_dict = {
            "command": "compileUnits",
            "type": "request",
            "arguments": args_dict,
        }
        response = self.send_recv(command_dict)
        return response

    def request_completions(self, text, frameId=None):
        args_dict = {"text": text, "column": len(text) + 1}
        if frameId:
            args_dict["frameId"] = frameId
        command_dict = {
            "command": "completions",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_modules(self, startModule: int, moduleCount: int):
        return self.send_recv(
            {
                "command": "modules",
                "type": "request",
                "arguments": {"startModule": startModule, "moduleCount": moduleCount},
            }
        )

    def request_stackTrace(
        self, threadId=None, startFrame=None, levels=None, format=None, dump=False
    ):
        if threadId is None:
            threadId = self.get_thread_id()
        args_dict = {"threadId": threadId}
        if startFrame is not None:
            args_dict["startFrame"] = startFrame
        if levels is not None:
            args_dict["levels"] = levels
        if format is not None:
            args_dict["format"] = format
        command_dict = {
            "command": "stackTrace",
            "type": "request",
            "arguments": args_dict,
        }
        response = self.send_recv(command_dict)
        if dump:
            for idx, frame in enumerate(response["body"]["stackFrames"]):
                name = frame["name"]
                if "line" in frame and "source" in frame:
                    source = frame["source"]
                    if "sourceReference" not in source:
                        if "name" in source:
                            source_name = source["name"]
                            line = frame["line"]
                            print("[%3u] %s @ %s:%u" % (idx, name, source_name, line))
                            continue
                print("[%3u] %s" % (idx, name))
        return response

    def request_source(self, sourceReference):
        """Request a source from a 'Source' reference."""
        command_dict = {
            "command": "source",
            "type": "request",
            "arguments": {
                "source": {"sourceReference": sourceReference},
                # legacy version of the request
                "sourceReference": sourceReference,
            },
        }
        return self.send_recv(command_dict)

    def request_threads(self):
        """Request a list of all threads and combine any information from any
        "stopped" events since those contain more information about why a
        thread actually stopped. Returns an array of thread dictionaries
        with information about all threads"""
        command_dict = {"command": "threads", "type": "request", "arguments": {}}
        response = self.send_recv(command_dict)
        if not response["success"]:
            self.threads = None
            return response
        body = response["body"]
        # Fill in "self.threads" correctly so that clients that call
        # self.get_threads() or self.get_thread_id(...) can get information
        # on threads when the process is stopped.
        if "threads" in body:
            self.threads = body["threads"]
            for thread in self.threads:
                # Copy the thread dictionary so we can add key/value pairs to
                # it without affecting the original info from the "threads"
                # command.
                tid = thread["id"]
                if tid in self.thread_stop_reasons:
                    thread_stop_info = self.thread_stop_reasons[tid]
                    copy_keys = ["reason", "description", "text"]
                    for key in copy_keys:
                        if key in thread_stop_info:
                            thread[key] = thread_stop_info[key]
        else:
            self.threads = None
        return response

    def request_variables(
        self, variablesReference, start=None, count=None, is_hex=None
    ):
        args_dict = {"variablesReference": variablesReference}
        if start is not None:
            args_dict["start"] = start
        if count is not None:
            args_dict["count"] = count
        if is_hex is not None:
            args_dict["format"] = {"hex": is_hex}
        command_dict = {
            "command": "variables",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_setVariable(self, containingVarRef, name, value, id=None):
        args_dict = {
            "variablesReference": containingVarRef,
            "name": name,
            "value": str(value),
        }
        if id is not None:
            args_dict["id"] = id
        command_dict = {
            "command": "setVariable",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_locations(self, locationReference):
        args_dict = {
            "locationReference": locationReference,
        }
        command_dict = {
            "command": "locations",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)

    def request_testGetTargetBreakpoints(self):
        """A request packet used in the LLDB test suite to get all currently
        set breakpoint infos for all breakpoints currently set in the
        target.
        """
        command_dict = {
            "command": "_testGetTargetBreakpoints",
            "type": "request",
            "arguments": {},
        }
        return self.send_recv(command_dict)

    def terminate(self):
        self.send.close()
        if self.recv_thread.is_alive():
            self.recv_thread.join()

    def request_setInstructionBreakpoints(self, memory_reference=[]):
        breakpoints = []
        for i in memory_reference:
            args_dict = {
                "instructionReference": i,
            }
            breakpoints.append(args_dict)
        args_dict = {"breakpoints": breakpoints}
        command_dict = {
            "command": "setInstructionBreakpoints",
            "type": "request",
            "arguments": args_dict,
        }
        return self.send_recv(command_dict)


class DebugAdapterServer(DebugCommunication):
    def __init__(
        self,
        executable: Optional[str] = None,
        connection: Optional[str] = None,
        init_commands: list[str] = [],
        log_file: Optional[TextIO] = None,
        env: Optional[dict[str, str]] = None,
    ):
        self.process = None
        self.connection = None
        if executable is not None:
            process, connection = DebugAdapterServer.launch(
                executable=executable, connection=connection, env=env, log_file=log_file
            )
            self.process = process
            self.connection = connection

        if connection is not None:
            scheme, address = connection.split("://")
            if scheme == "unix-connect":  # unix-connect:///path
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(address)
            elif scheme == "connection":  # connection://[host]:port
                host, port = address.rsplit(":", 1)
                # create_connection with try both ipv4 and ipv6.
                s = socket.create_connection((host.strip("[]"), int(port)))
            else:
                raise ValueError("invalid connection: {}".format(connection))
            DebugCommunication.__init__(
                self, s.makefile("rb"), s.makefile("wb"), init_commands, log_file
            )
            self.connection = connection
        else:
            DebugCommunication.__init__(
                self, self.process.stdout, self.process.stdin, init_commands, log_file
            )

    @classmethod
    def launch(
        cls,
        *,
        executable: str,
        env: Optional[dict[str, str]] = None,
        log_file: Optional[TextIO] = None,
        connection: Optional[str] = None,
    ) -> tuple[subprocess.Popen, Optional[str]]:
        adapter_env = os.environ.copy()
        if env is not None:
            adapter_env.update(env)

        if log_file:
            adapter_env["LLDBDAP_LOG"] = log_file
        args = [executable]

        if connection is not None:
            args.append("--connection")
            args.append(connection)

        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=adapter_env,
        )

        if connection is None:
            return (process, None)

        # lldb-dap will print the listening address once the listener is
        # made to stdout. The listener is formatted like
        # `connection://host:port` or `unix-connection:///path`.
        expected_prefix = "Listening for: "
        out = process.stdout.readline().decode()
        if not out.startswith(expected_prefix):
            process.kill()
            raise ValueError(
                "lldb-dap failed to print listening address, expected '{}', got '{}'".format(
                    expected_prefix, out
                )
            )

        # If the listener expanded into multiple addresses, use the first.
        connection = out.removeprefix(expected_prefix).rstrip("\r\n").split(",", 1)[0]

        return (process, connection)

    def get_pid(self) -> int:
        if self.process:
            return self.process.pid
        return -1

    def terminate(self):
        try:
            if self.process is not None:
                process = self.process
                self.process = None
                try:
                    # When we close stdin it should signal the lldb-dap that no
                    # new messages will arrive and it should shutdown on its
                    # own.
                    process.stdin.close()
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if process.returncode != 0:
                    raise DebugAdapterProcessError(process.returncode)
        finally:
            super(DebugAdapterServer, self).terminate()


class DebugAdapterError(Exception):
    pass


class DebugAdapterProcessError(DebugAdapterError):
    """Raised when the lldb-dap process exits with a non-zero exit status."""

    def __init__(self, returncode):
        self.returncode = returncode

    def __str__(self):
        if self.returncode and self.returncode < 0:
            try:
                return f"lldb-dap died with {signal.Signals(-self.returncode).name}."
            except ValueError:
                return f"lldb-dap died with unknown signal {-self.returncode}."
        else:
            return f"lldb-dap returned non-zero exit status {self.returncode}."


def attach_options_specified(options):
    if options.pid is not None:
        return True
    if options.waitFor:
        return True
    if options.attach:
        return True
    if options.attachCmds:
        return True
    return False


def run_vscode(dbg, args, options):
    dbg.request_initialize(options.sourceInitFile)

    if options.sourceBreakpoints:
        source_to_lines = {}
        for file_line in options.sourceBreakpoints:
            (path, line) = file_line.split(":")
            if len(path) == 0 or len(line) == 0:
                print('error: invalid source with line "%s"' % (file_line))

            else:
                if path in source_to_lines:
                    source_to_lines[path].append(int(line))
                else:
                    source_to_lines[path] = [int(line)]
        for source in source_to_lines:
            dbg.request_setBreakpoints(Source(source), source_to_lines[source])
    if options.funcBreakpoints:
        dbg.request_setFunctionBreakpoints(options.funcBreakpoints)

    dbg.request_configurationDone()

    if attach_options_specified(options):
        response = dbg.request_attach(
            program=options.program,
            pid=options.pid,
            waitFor=options.waitFor,
            attachCommands=options.attachCmds,
            initCommands=options.initCmds,
            preRunCommands=options.preRunCmds,
            stopCommands=options.stopCmds,
            exitCommands=options.exitCmds,
            terminateCommands=options.terminateCmds,
        )
    else:
        response = dbg.request_launch(
            options.program,
            args=args,
            env=options.envs,
            cwd=options.workingDir,
            debuggerRoot=options.debuggerRoot,
            sourcePath=options.sourcePath,
            initCommands=options.initCmds,
            preRunCommands=options.preRunCmds,
            stopCommands=options.stopCmds,
            exitCommands=options.exitCmds,
            terminateCommands=options.terminateCmds,
        )

    if response["success"]:
        dbg.wait_for_stopped()
    else:
        if "message" in response:
            print(response["message"])
    dbg.request_disconnect(terminateDebuggee=True)


def main():
    parser = optparse.OptionParser(
        description=(
            "A testing framework for the Visual Studio Code Debug Adapter protocol"
        )
    )

    parser.add_option(
        "--vscode",
        type="string",
        dest="vscode_path",
        help=(
            "The path to the command line program that implements the "
            "Visual Studio Code Debug Adapter protocol."
        ),
        default=None,
    )

    parser.add_option(
        "--program",
        type="string",
        dest="program",
        help="The path to the program to debug.",
        default=None,
    )

    parser.add_option(
        "--workingDir",
        type="string",
        dest="workingDir",
        default=None,
        help="Set the working directory for the process we launch.",
    )

    parser.add_option(
        "--sourcePath",
        type="string",
        dest="sourcePath",
        default=None,
        help=(
            "Set the relative source root for any debug info that has "
            "relative paths in it."
        ),
    )

    parser.add_option(
        "--debuggerRoot",
        type="string",
        dest="debuggerRoot",
        default=None,
        help=(
            "Set the working directory for lldb-dap for any object files "
            "with relative paths in the Mach-o debug map."
        ),
    )

    parser.add_option(
        "-r",
        "--replay",
        type="string",
        dest="replay",
        help=(
            "Specify a file containing a packet log to replay with the "
            "current Visual Studio Code Debug Adapter executable."
        ),
        default=None,
    )

    parser.add_option(
        "-g",
        "--debug",
        action="store_true",
        dest="debug",
        default=False,
        help="Pause waiting for a debugger to attach to the debug adapter",
    )

    parser.add_option(
        "--sourceInitFile",
        action="store_true",
        dest="sourceInitFile",
        default=False,
        help="Whether lldb-dap should source .lldbinit file or not",
    )

    parser.add_option(
        "--connection",
        dest="connection",
        help="Attach a socket connection of using STDIN for VSCode",
        default=None,
    )

    parser.add_option(
        "--pid",
        type="int",
        dest="pid",
        help="The process ID to attach to",
        default=None,
    )

    parser.add_option(
        "--attach",
        action="store_true",
        dest="attach",
        default=False,
        help=(
            "Specify this option to attach to a process by name. The "
            "process name is the basename of the executable specified with "
            "the --program option."
        ),
    )

    parser.add_option(
        "-f",
        "--function-bp",
        type="string",
        action="append",
        dest="funcBreakpoints",
        help=(
            "Specify the name of a function to break at. "
            "Can be specified more than once."
        ),
        default=[],
    )

    parser.add_option(
        "-s",
        "--source-bp",
        type="string",
        action="append",
        dest="sourceBreakpoints",
        default=[],
        help=(
            "Specify source breakpoints to set in the format of "
            "<source>:<line>. "
            "Can be specified more than once."
        ),
    )

    parser.add_option(
        "--attachCommand",
        type="string",
        action="append",
        dest="attachCmds",
        default=[],
        help=(
            "Specify a LLDB command that will attach to a process. "
            "Can be specified more than once."
        ),
    )

    parser.add_option(
        "--initCommand",
        type="string",
        action="append",
        dest="initCmds",
        default=[],
        help=(
            "Specify a LLDB command that will be executed before the target "
            "is created. Can be specified more than once."
        ),
    )

    parser.add_option(
        "--preRunCommand",
        type="string",
        action="append",
        dest="preRunCmds",
        default=[],
        help=(
            "Specify a LLDB command that will be executed after the target "
            "has been created. Can be specified more than once."
        ),
    )

    parser.add_option(
        "--stopCommand",
        type="string",
        action="append",
        dest="stopCmds",
        default=[],
        help=(
            "Specify a LLDB command that will be executed each time the"
            "process stops. Can be specified more than once."
        ),
    )

    parser.add_option(
        "--exitCommand",
        type="string",
        action="append",
        dest="exitCmds",
        default=[],
        help=(
            "Specify a LLDB command that will be executed when the process "
            "exits. Can be specified more than once."
        ),
    )

    parser.add_option(
        "--terminateCommand",
        type="string",
        action="append",
        dest="terminateCmds",
        default=[],
        help=(
            "Specify a LLDB command that will be executed when the debugging "
            "session is terminated. Can be specified more than once."
        ),
    )

    parser.add_option(
        "--env",
        type="string",
        action="append",
        dest="envs",
        default=[],
        help=("Specify environment variables to pass to the launched " "process."),
    )

    parser.add_option(
        "--waitFor",
        action="store_true",
        dest="waitFor",
        default=False,
        help=(
            "Wait for the next process to be launched whose name matches "
            "the basename of the program specified with the --program "
            "option"
        ),
    )

    (options, args) = parser.parse_args(sys.argv[1:])

    if options.vscode_path is None and options.connection is None:
        print(
            "error: must either specify a path to a Visual Studio Code "
            "Debug Adapter vscode executable path using the --vscode "
            "option, or using the --connection option"
        )
        return
    dbg = DebugAdapterServer(
        executable=options.vscode_path, connection=options.connection
    )
    if options.debug:
        raw_input('Waiting for debugger to attach pid "%i"' % (dbg.get_pid()))
    if options.replay:
        dbg.replay_packets(options.replay)
    else:
        run_vscode(dbg, args, options)
    dbg.terminate()


if __name__ == "__main__":
    main()
