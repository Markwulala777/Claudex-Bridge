#!/usr/bin/env python3
"""Read-only app-server transport for one already-owned Codex Desktop thread.

This adapter never starts/resumes an app-server execution or forwards mutations.
It translates the existing desktop owner's snapshots into native TUI protocol
history and notifications. It is a version-pinned development adapter, not a
replacement app-server and not a completed cross-platform bridge.
"""
import argparse
import base64
import copy
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import time
import uuid

sys.dont_write_bytecode = True
from desktop_bridge import BridgeError, Desktop, codex_home, turns, validate_rollout, verify_installation


TUI_VERSION = "0.163.0-alpha.11.1"
MAX_FRAME = 32 * 1024 * 1024
TERMINAL = {"completed", "failed", "interrupted"}
# Native thread items, not desktop-only presentation decorations.
ITEM_TYPES = {
    "userMessage", "hookPrompt", "agentMessage", "functionCallOutput", "plan", "reasoning",
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall",
    "subAgentActivity", "webSearch", "imageView", "sleep", "imageGeneration",
    "enteredReviewMode", "exitedReviewMode", "contextCompaction",
}


class ProtocolError(RuntimeError):
    def __init__(self, message, code=-32602):
        super().__init__(message)
        self.code = code


def read_session_meta(path, thread_id):
    """Inspect only the supplied rollout header, never scan other conversations."""
    with Path(path).open("rb") as stream:
        raw = stream.readline(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise BridgeError("Rollout header exceeds the limit")
    event = json.loads(raw)
    meta = event.get("payload", {})
    if event.get("type") != "session_meta" or meta.get("id") != thread_id:
        raise BridgeError("Rollout header does not identify the requested thread")
    return meta


class ExistingDesktopSource:
    """Use the existing owner only; deliberately do not call Desktop.attach()."""
    def __init__(self, thread_id, rollout):
        self.thread_id = str(uuid.UUID(thread_id))
        self.rollout = validate_rollout(rollout)
        self.meta = read_session_meta(self.rollout, self.thread_id)
        self.codex_home = str(codex_home())
        self.desktop = None

    def __enter__(self):
        verify_installation()
        # Do not write transcripts or model output to an auxiliary log.
        self.desktop = Desktop(io.StringIO())
        self.desktop.__enter__()
        try:
            self.desktop.thread_id = self.thread_id
            reply = self.desktop.request("thread-owner-discovery", {
                "hostId": "local", "conversationId": self.thread_id,
            }, timeout=5)
            self.desktop.owner = reply["handledByClientId"]
            self.desktop.follow(True)
            self.read()
            return self
        except BaseException:
            self.desktop.__exit__(*sys.exc_info())
            raise

    def read(self):
        # This read-only follower request hydrates the owner's existing history.
        # No thread/resume, turn/start, daemon or second execution is invoked.
        state = self.desktop.snapshot()
        if state.get("id") != self.thread_id:
            raise BridgeError("Owner snapshot belongs to a different thread")
        # Desktop logs can be large. The in-memory sink is intentionally cleared.
        self.desktop.log.seek(0)
        self.desktop.log.truncate()
        return copy.deepcopy(state)

    def __exit__(self, *args):
        if self.desktop is not None:
            self.desktop.__exit__(*args)


def timestamp_seconds(value, fallback=0):
    if isinstance(value, (int, float)):
        return int(value / 1000 if value > 100_000_000_000 else value)
    if isinstance(value, str):
        try:
            return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return fallback


def normalise_item(item):
    if not isinstance(item, dict) or not item.get("id"):
        raise ProtocolError("Unsupported desktop item shape; refusing to fabricate native history")
    kind = item.get("type")
    if kind == "steeringUserMessage":
        # The desktop replaces an acknowledged native userMessage with its
        # optimistic form plus a `steered` position marker. Restore the server
        # identity only after the owner has acknowledged the original input.
        if item.get("status") != "accepted":
            return None
        if not item.get("serverUserMessageId") or not isinstance(item.get("input"), list):
            raise ProtocolError("Accepted desktop steering input is missing its original identity")
        return {"type": "userMessage", "id": item["serverUserMessageId"],
                "content": copy.deepcopy(item["input"]),
                "clientId": item.get("serverClientUserMessageId", item.get("clientUserMessageId"))}
    if kind == "userInputResponse":
        # Native history has no desktop answer-form item. Preserve only actual
        # submitted human answer text as user content; do not invent an assistant
        # response or replay an interactive request in this read-only consumer.
        if not item.get("completed"):
            return None
        answers = item.get("answers") or {}
        if not isinstance(answers, dict):
            raise ProtocolError("Unsupported desktop answer shape")
        content = []
        for values in answers.values():
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ProtocolError("Unsupported desktop answer text")
            content.extend({"type": "text", "text": value, "text_elements": []} for value in values)
        return {"type": "userMessage", "id": item["id"], "content": content, "clientId": None} if content else None
    if kind in {"steered", "error"}:
        # `steered` is only the position marker for the restored userMessage;
        # `error` is an app-server notification, replayed separately when new.
        return None
    if kind not in ITEM_TYPES:
        raise ProtocolError("Unsupported desktop item type: " + repr(kind))
    return copy.deepcopy(item)


def normalise_turn(turn):
    status = turn.get("status")
    if status not in TERMINAL | {"inProgress"}:
        raise ProtocolError("Unsupported original turn status: " + repr(status))
    items = []
    seen = set()
    for original in turn.get("items", []):
        item = normalise_item(original)
        if item is not None and item["id"] not in seen:
            items.append(item)
            seen.add(item["id"])
    params = turn.get("params", {})
    # Desktop keeps the initial user input in turn.params, whereas app-server
    # ThreadItem history carries it as a userMessage. Never invent message text.
    opening = any(item["type"] == "userMessage" and
                  (item.get("content") == params.get("input") or
                   (params.get("clientUserMessageId") is not None and
                    item.get("clientId") == params["clientUserMessageId"])) for item in items)
    if params.get("input") and not opening:
        items.insert(0, {"type": "userMessage", "id": "user-" + turn["turnId"],
                         "content": copy.deepcopy(params["input"]),
                         "clientId": params.get("clientUserMessageId")})
    error = turn.get("error")
    if error is not None and not isinstance(error, dict):
        error = {"message": str(error), "codexErrorInfo": None, "additionalDetails": None}
    return {"id": turn["turnId"], "items": items, "status": status, "error": error,
            "startedAt": timestamp_seconds(turn.get("turnStartedAtMs")) or None,
            "completedAt": timestamp_seconds(turn.get("completedAtMs")) or None,
            "durationMs": turn.get("durationMs"), "rootTurnId": turn.get("rootTurnId"),
            "itemsView": "full"}


class Mirror:
    def __init__(self, source):
        self.source = source
        self.raw = None
        self.thread = None
        self.refresh()

    def refresh(self):
        state = self.source.read()
        if state.get("id") != self.source.thread_id:
            raise ProtocolError("Snapshot identity changed")
        meta = self.source.meta
        history = [normalise_turn(turn) for turn in turns(state)]
        settings = state.get("latestThreadSettings") or {}
        model = settings.get("model") or state.get("latestModel") or meta.get("model")
        provider = state.get("modelProvider") or meta.get("model_provider")
        cwd = state.get("cwd") or meta.get("cwd")
        if not model or not provider or not cwd or not Path(cwd).is_absolute():
            raise ProtocolError("Original owner is missing model, provider or absolute working directory")
        created = timestamp_seconds(meta.get("timestamp"))
        active = any(turn["status"] == "inProgress" for turn in history)
        thread = {
            "id": self.source.thread_id, "sessionId": meta.get("session_id") or self.source.thread_id,
            "preview": state.get("title") or "",
            # An unnamed remote thread makes TUI try an ephemeral title model.
            # This view-only fallback suppresses that request without saving a title.
            "name": state.get("title") or "Read-only view " + self.source.thread_id,
            "model": model, "modelProvider": provider, "cwd": cwd,
            "cliVersion": meta.get("cli_version") or "unknown",
            "createdAt": created, "updatedAt": timestamp_seconds(state.get("updatedAt"), created),
            "source": meta.get("source") or "unknown", "originator": meta.get("originator"),
            "ephemeral": False, "projectId": state.get("projectId"),
            "path": str(self.source.rollout), "historyMode": "legacy", "turns": history,
            "status": {"type": "active", "activeFlags": []} if active else {"type": "idle"},
            # The gateway is a passive native view. All input stays in the owner.
            "canAcceptDirectInput": False,
            "reasoningEffort": settings.get("effort", state.get("latestReasoningEffort")),
        }
        self.raw, self.thread = state, thread
        return thread

    def resume_response(self):
        settings = self.raw.get("latestThreadSettings") or {}
        permissions = self.raw.get("currentPermissions") or {}
        approval = settings.get("approvalPolicy", permissions.get("approvalPolicy"))
        reviewer = settings.get("approvalsReviewer", permissions.get("approvalsReviewer"))
        sandbox = settings.get("sandboxPolicy", permissions.get("sandboxPolicy"))
        if approval is None or reviewer is None or sandbox is None:
            raise ProtocolError("Original owner does not expose complete permission metadata")
        return {"thread": copy.deepcopy(self.thread), "model": self.thread["model"],
                "modelProvider": self.thread["modelProvider"], "cwd": self.thread["cwd"],
                "reasoningEffort": self.thread["reasoningEffort"],
                "serviceTier": settings.get("serviceTier"), "approvalPolicy": approval,
                "approvalsReviewer": reviewer, "sandbox": sandbox,
                "activePermissionProfile": permissions.get("activePermissionProfile"),
                "runtimeWorkspaceRoots": permissions.get("runtimeWorkspaceRoots", []),
                "collaborationMode": settings.get("collaborationMode"),
                "multiAgentMode": "explicitRequestOnly"}

    def item_complete(self, turn_id, item):
        original = next(turn for turn in turns(self.raw) if turn["turnId"] == turn_id)
        anchors = original.get("completedItemIds", {})
        if item["id"] in anchors:
            return bool(anchors[item["id"]])
        return (item["type"] == "userMessage" or original["status"] in TERMINAL
                or item.get("status") in TERMINAL | {"declined"})


def notification(method, **params):
    return {"method": method, "params": params}


class Subscription:
    """Translate only changes observed in authenticated original-owner snapshots."""
    def __init__(self, mirror):
        self.previous = copy.deepcopy(mirror.thread)
        self.completed = {(turn["id"], item["id"]) for turn in mirror.thread["turns"]
                          for item in turn["items"] if mirror.item_complete(turn["id"], item)}
        # Historic transient retry banners are not current failures. New source
        # errors retain the native notification type and original error fields.
        self.errors = {(turn["turnId"], item["id"]) for turn in turns(mirror.raw)
                       for item in turn.get("items", []) if item.get("type") == "error"}

    def changes(self, mirror):
        current = mirror.thread
        thread_id = current["id"]
        previous_turns = {turn["id"]: turn for turn in self.previous["turns"]}
        events = []
        if current["status"] != self.previous["status"]:
            events.append(notification("thread/status/changed", threadId=thread_id, status=current["status"]))
        for turn in current["turns"]:
            old = previous_turns.get(turn["id"])
            if old is None:
                events.append(notification("turn/started", threadId=thread_id,
                                           turn={**turn, "items": [], "status": "inProgress"}))
            old_items = {item["id"]: item for item in (old or {}).get("items", [])}
            for item in turn["items"]:
                before = old_items.get(item["id"])
                common = {"threadId": thread_id, "turnId": turn["id"]}
                delta_fields = {"agentMessage": ("text", "item/agentMessage/delta"),
                                "plan": ("text", "item/plan/delta"),
                                "commandExecution": ("aggregatedOutput", "item/commandExecution/outputDelta")}
                field, method = delta_fields.get(item["type"], (None, None))
                if before is None:
                    initial = copy.deepcopy(item)
                    if field:
                        initial[field] = ""
                    if item["type"] == "reasoning":
                        initial["summary"], initial["content"] = [], []
                    events.append(notification("item/started", **common, item=initial,
                                               startedAtMs=int(time.time() * 1000)))
                if field:
                    text = item.get(field) or ""
                    old_text = (before or {}).get(field) or ""
                    if not text.startswith(old_text):
                        raise ProtocolError("Owner rewrote a streamed item; reconnect to replay authoritative history")
                    if text != old_text:
                        events.append(notification(method, **common, itemId=item["id"], delta=text[len(old_text):]))
                if item["type"] == "reasoning":
                    for field, method, index_name in (("summary", "item/reasoning/summaryTextDelta", "summaryIndex"),
                                                       ("content", "item/reasoning/textDelta", "contentIndex")):
                        for index, text in enumerate(item.get(field, [])):
                            earlier = (before or {}).get(field, [])
                            old_text = earlier[index] if index < len(earlier) else ""
                            if field == "summary" and index >= len(earlier):
                                events.append(notification("item/reasoning/summaryPartAdded", **common,
                                                           itemId=item["id"], summaryIndex=index))
                            if not text.startswith(old_text):
                                raise ProtocolError("Owner rewrote reasoning; reconnect for authoritative history")
                            if text != old_text:
                                events.append(notification(method, **common, itemId=item["id"],
                                                           delta=text[len(old_text):], **{index_name: index}))
                key = (turn["id"], item["id"])
                if mirror.item_complete(turn["id"], item) and key not in self.completed:
                    events.append(notification("item/completed", **common, item=item,
                                               completedAtMs=int(time.time() * 1000)))
                    self.completed.add(key)
            if turn["status"] in TERMINAL and (old is None or old["status"] not in TERMINAL):
                events.append(notification("turn/completed", threadId=thread_id, turn=turn))
        for turn in turns(mirror.raw):
            for item in turn.get("items", []):
                key = (turn["turnId"], item["id"])
                if item.get("type") != "error" or key in self.errors:
                    continue
                if not isinstance(item.get("message"), str) or type(item.get("willRetry")) is not bool:
                    raise ProtocolError("Unsupported original error notification")
                events.append(notification("error", threadId=thread_id, turnId=turn["turnId"],
                                           willRetry=item["willRetry"], error={
                                               "message": item["message"], "codexErrorInfo": item.get("errorInfo"),
                                               "additionalDetails": item.get("additionalDetails")}))
                self.errors.add(key)
        self.previous = copy.deepcopy(current)
        return events


class RPC:
    def __init__(self, mirror):
        self.mirror = mirror
        self.initialized = False
        self.subscription = None

    def dispatch(self, method, params):
        params = params or {}
        if method == "initialize":
            if self.initialized:
                raise ProtocolError("Already initialized")
            info = params.get("clientInfo", {})
            if info.get("name") != "codex-tui" or info.get("version") != TUI_VERSION:
                raise ProtocolError("This adapter has only been checked with codex-tui " + TUI_VERSION)
            self.initialized = True
            return {"userAgent": "claudex-read-only/" + TUI_VERSION, "platformFamily": "unix",
                    "platformOs": "linux", "codexHome": self.mirror.source.codex_home}
        if not self.initialized:
            raise ProtocolError("Initialize first")
        if method == "account/read":
            # This local read-only transport needs no credentials and does not
            # expose or claim to be the original owner's account endpoint.
            if params.get("refreshToken"):
                raise ProtocolError("The read-only gateway cannot refresh credentials")
            return {"account": None, "requiresOpenaiAuth": False}
        if method == "model/list":
            name = self.mirror.thread["model"]
            effort = self.mirror.thread["reasoningEffort"] or "none"
            return {"data": [{"id": name, "model": name, "displayName": name,
                              "description": "Original owner model; this gateway is read-only",
                              "hidden": False, "isDefault": True,
                              "defaultReasoningEffort": effort, "supportedReasoningEfforts": []}],
                    "nextCursor": None}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method == "collaborationMode/list":
            return {"data": []}
        if method == "config/read":
            return {"config": {"model": self.mirror.thread["model"],
                               "model_provider": self.mirror.thread["modelProvider"]},
                    "origins": {}, "layers": []}
        if method in ("thread/read", "thread/resume", "thread/unsubscribe", "thread/turns/list"):
            if params.get("threadId") != self.mirror.source.thread_id:
                raise ProtocolError("This gateway only exposes its explicitly selected original thread")
            if method == "thread/unsubscribe":
                self.subscription = None
                return {}
            if method == "thread/read":
                thread = copy.deepcopy(self.mirror.thread)
                if not params.get("includeTurns"):
                    thread["turns"] = []
                return {"thread": thread}
            if method == "thread/turns/list":
                direction = params.get("sortDirection") or "desc"
                if direction not in ("asc", "desc"):
                    raise ProtocolError("Unknown turn sort direction")
                history = copy.deepcopy(self.mirror.thread["turns"])
                if direction == "desc":
                    history.reverse()
                cursor = params.get("cursor")
                if cursor:
                    prefix = direction + ":"
                    if not isinstance(cursor, str) or not cursor.startswith(prefix):
                        raise ProtocolError("Unknown turn cursor")
                    identifiers = [turn["id"] for turn in history]
                    if cursor[len(prefix):] not in identifiers:
                        raise ProtocolError("Turn cursor is no longer in original owner history")
                    history = history[identifiers.index(cursor[len(prefix):]) + 1:]
                limit = params.get("limit") or 50
                if type(limit) is not int or not 1 <= limit <= 1000:
                    raise ProtocolError("Invalid turn limit")
                page = history[:limit]
                return {"data": page, "nextCursor": direction + ":" + page[-1]["id"] if len(history) > limit else None,
                        "backwardsCursor": None}
            # The native TUI includes serviceTier from local config even when
            # merely opening a remote view. A matching value is descriptive,
            # not an execution override. Return the owner's value and never
            # forward a resume/mutation. Conflicting settings still fail closed.
            requested_tier = params.get("serviceTier")
            owner_tier = (self.mirror.raw.get("latestThreadSettings") or {}).get("serviceTier")
            if requested_tier is not None and requested_tier != owner_tier:
                raise ProtocolError("Read-only resume cannot change serviceTier; use the original owner's setting")
            overrides = {key: value for key, value in params.items()
                         if key not in {"threadId", "excludeTurns", "initialTurnsPage", "serviceTier"}
                         and value is not None and value != {}}
            if overrides:
                raise ProtocolError("Read-only resume refuses execution overrides: " + ", ".join(sorted(overrides)))
            result = self.mirror.resume_response()
            self.subscription = Subscription(self.mirror)
            if params.get("excludeTurns"):
                result["thread"]["turns"] = []
            return result
        if method == "skills/list":
            return {"data": []}
        if method == "plugin/list":
            return {"marketplaces": [], "featuredPluginIds": [], "marketplaceLoadErrors": []}
        if method == "mcpServerStatus/list":
            return {"data": [], "nextCursor": None}
        if method == "experimentalFeature/list":
            return {"data": [], "nextCursor": None}
        if method == "hooks/list":
            return {"data": []}
        if method == "thread/loaded/list":
            return {"data": [self.mirror.source.thread_id], "nextCursor": None}
        if method == "thread/list":
            # The adapter exposes one selected conversation, never the account's
            # unrelated history. Unsupported filters produce no matches.
            thread = self.mirror.thread
            hidden = (params.get("archived") or params.get("sectionId") or params.get("cursor")
                      or params.get("parentThreadId") or params.get("ancestorThreadId")
                      or thread["id"] in (params.get("excludedThreadIds") or [])
                      or (params.get("cwd") and params["cwd"] != thread["cwd"])
                      or (params.get("modelProviders") and thread["modelProvider"] not in params["modelProviders"])
                      or (params.get("sourceKinds") and thread["source"] not in params["sourceKinds"])
                      or params.get("searchTerm") or params.get("originators"))
            return {"data": [] if hidden else [{**thread, "turns": []}], "nextCursor": None}
        raise ProtocolError("Read-only gateway does not support " + method + "; use the original Desktop owner", -32601)

    def handle(self, message):
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            raise ProtocolError("Expected a JSON-RPC request object")
        if "id" not in message:
            if message["method"] != "initialized":
                raise ProtocolError("Unsupported client notification")
            return None
        try:
            result = self.dispatch(message["method"], message.get("params"))
            return {"id": message["id"], "result": result}
        except (ProtocolError, ValueError, TypeError, KeyError) as exc:
            return {"id": message["id"], "error": {
                "code": getattr(exc, "code", -32602), "message": str(exc)}}


class WebSocket:
    """Small RFC 6455 server transport: masked text frames and bounded buffering."""
    def __init__(self, connection):
        self.socket = connection
        self.buffer = bytearray()
        self.fragments = bytearray()
        self.fragmented = False

    def handshake(self):
        self.socket.settimeout(5)
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            data = self.socket.recv(4096)
            if not data:
                raise EOFError()
            raw.extend(data)
            if len(raw) > 16384:
                raise ProtocolError("HTTP upgrade header too large")
        header, tail = bytes(raw).split(b"\r\n\r\n", 1)
        lines = header.decode("ascii").split("\r\n")
        if lines[0] not in ("GET /rpc HTTP/1.1", "GET / HTTP/1.1"):
            raise ProtocolError("Unsupported WebSocket path")
        headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
        if (headers.get("upgrade", "").lower() != "websocket" or
                headers.get("sec-websocket-version") != "13" or "origin" in headers):
            raise ProtocolError("Only the local native TUI WebSocket client is supported")
        key = headers.get("sec-websocket-key", "")
        if len(base64.b64decode(key, validate=True)) != 16:
            raise ProtocolError("Invalid WebSocket key")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        self.socket.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode("ascii"))
        self.buffer.extend(tail)
        self.socket.settimeout(5)

    def send_frame(self, payload, opcode=1):
        if len(payload) > MAX_FRAME:
            raise ProtocolError("WebSocket frame exceeds the limit")
        prefix = bytes([0x80 | opcode])
        if len(payload) < 126:
            prefix += bytes([len(payload)])
        elif len(payload) <= 65535:
            prefix += bytes([126]) + struct.pack("!H", len(payload))
        else:
            prefix += bytes([127]) + struct.pack("!Q", len(payload))
        self.socket.sendall(prefix + payload)

    def send(self, value):
        self.send_frame(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def receive(self, timeout=0.2):
        deadline = time.monotonic() + timeout
        while True:
            frame = self.take_frame()
            if frame is not None:
                final, opcode, data = frame
                if opcode == 8:
                    raise EOFError()
                if opcode == 9:
                    self.send_frame(data, 10)
                    continue
                if opcode == 10:
                    continue
                if opcode not in (0, 1) or (opcode == 0) != self.fragmented:
                    raise ProtocolError("Unsupported WebSocket frame sequence")
                self.fragments.extend(data)
                if len(self.fragments) > MAX_FRAME:
                    raise ProtocolError("WebSocket message exceeds the limit")
                self.fragmented = not final
                if final:
                    raw = bytes(self.fragments)
                    self.fragments.clear()
                    return json.loads(raw)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.socket], [], [], remaining)[0]:
                return None
            data = self.socket.recv(65536)
            if not data:
                raise EOFError()
            self.buffer.extend(data)
            if len(self.buffer) > MAX_FRAME + 14:
                raise ProtocolError("WebSocket receive buffer exceeds the limit")

    def take_frame(self):
        if len(self.buffer) < 2:
            return None
        first, second = self.buffer[:2]
        if first & 0x70 or not second & 0x80:
            raise ProtocolError("Client frames must be masked without extensions")
        size = second & 127
        offset = 2
        if size in (126, 127):
            width = 2 if size == 126 else 8
            if len(self.buffer) < offset + width:
                return None
            size = int.from_bytes(self.buffer[offset:offset + width], "big")
            offset += width
        if size > MAX_FRAME or ((first & 15) >= 8 and (not first & 0x80 or size > 125)):
            raise ProtocolError("Invalid WebSocket frame length")
        if len(self.buffer) < offset + 4 + size:
            return None
        mask = self.buffer[offset:offset + 4]
        offset += 4
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(self.buffer[offset:offset + size]))
        del self.buffer[:offset + size]
        return bool(first & 0x80), first & 15, data


def serve_connection(connection, mirror, poll_interval=0.5, on_request=None):
    websocket = WebSocket(connection)
    websocket.handshake()
    rpc = RPC(mirror)
    next_poll = time.monotonic() + poll_interval
    while True:
        request = websocket.receive(min(poll_interval, 0.2))
        if request is not None:
            if on_request:
                on_request(copy.deepcopy(request))
            reply = rpc.handle(request)
            if reply is not None:
                websocket.send(reply)
        if rpc.subscription is not None and time.monotonic() >= next_poll:
            # A failed owner read closes the view; it never manufactures completion.
            mirror.refresh()
            for event in rpc.subscription.changes(mirror):
                websocket.send(event)
            next_poll = time.monotonic() + poll_interval


def serve_unix(path, mirror):
    path = Path(path).expanduser().absolute()
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise BridgeError("Use a private, user-owned directory for the gateway socket")
    if path.exists() or path.is_symlink():
        raise BridgeError("Gateway socket path already exists; refusing to remove an unrelated listener")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    identity = None
    try:
        listener.bind(str(path))
        bound = True
        identity = path.stat()
        os.chmod(path, 0o600)
        listener.listen(1)
        print(json.dumps({"endpoint": "unix://" + str(path), "thread_id": mirror.source.thread_id,
                          "mode": "read_only_original_owner", "tui_version": TUI_VERSION}), flush=True)
        connection, _ = listener.accept()
        with connection:
            if hasattr(socket, "SO_PEERCRED"):
                _, peer_uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer_uid != os.getuid():
                    raise BridgeError("Gateway client belongs to a different user")
            try:
                serve_connection(connection, mirror)
            except EOFError:
                pass
    finally:
        listener.close()
        if bound:
            try:
                remaining = path.lstat()
                if identity is not None and stat.S_ISSOCK(remaining.st_mode) and (remaining.st_dev, remaining.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True, help="Exact already-owned desktop thread UUID")
    parser.add_argument("--rollout", required=True, help="Matching original persisted rollout path")
    parser.add_argument("--socket", required=True, help="New Unix socket in an existing private directory")
    args = parser.parse_args(argv)
    try:
        with ExistingDesktopSource(args.thread, args.rollout) as source:
            serve_unix(args.socket, Mirror(source))
        return 0
    except (OSError, ValueError, BridgeError, ProtocolError, KeyboardInterrupt) as exc:
        print("Gateway stopped; original execution was not changed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
