import asyncio
import base64
import hashlib
import json
import re
import struct
from pathlib import Path

VERSION = 1
MAX_FRAME = 24 * 1024 * 1024
MAX_ENGINE_FRAME = 96 * 1024 * 1024
MAX_TL = 16 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


class ProtocolError(ValueError):
    pass


def encode(frame):
    data = json.dumps(frame, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ProtocolError("frame too large")
    return data


async def read(reader, max_frame=MAX_FRAME):
    try:
        data = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as error:
        raise ProtocolError("frame too large") from error
    if not data:
        raise EOFError
    if len(data) > max_frame or not data.endswith(b"\n"):
        raise ProtocolError("invalid frame")
    try:
        frame = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise ProtocolError("invalid JSON") from error
    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
        raise ProtocolError("expected an object with a type")
    return frame


def payload(value):
    if not isinstance(value, str) or len(value) > (MAX_TL + 2) // 3 * 4:
        raise ProtocolError("invalid TL payload")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ProtocolError("invalid base64") from error
    if not data or len(data) > MAX_TL or len(data) % 4:
        raise ProtocolError("unaligned or oversized TL payload")
    return data


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProtocolError("invalid identifier")
    return value


def counter(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,17}", value):
        raise ProtocolError("invalid sequence")
    return int(value)


def request_digest(dc, body, after):
    return hashlib.sha256(struct.pack("<i", dc) + body + after.encode()).hexdigest()


class Schema:
    def __init__(self, root):
        root = Path(root)
        api = (root / "api.tl").read_bytes()
        mtp = (root / "mtproto.tl").read_bytes()
        self.fingerprint = hashlib.sha256(api + b"\0" + mtp).hexdigest()
        self.methods = {}
        for section in (api, mtp):
            text = section.decode().split("---functions---", 1)[1]
            for name, constructor in re.findall(r"^([\w.]+)#([0-9a-f]+)\b", text, re.M):
                self.methods[int(constructor, 16)] = name

    def method(self, body):
        result = self.methods.get(struct.unpack_from("<I", body)[0])
        if result is None:
            raise ProtocolError("unknown or wrapped method")
        return result


READ_CACHE = frozenset({
    "help.getConfig", "help.getAppConfig", "help.getNearestDc",
    "users.getUsers", "users.getFullUser", "contacts.getContacts",
    "messages.getDialogs", "messages.getHistory", "messages.getMessages",
    "messages.getPeerDialogs", "messages.getChats", "messages.getFullChat",
    "messages.getDialogFilters", "messages.getPinnedDialogs",
    "messages.getAllStickers", "messages.getRecentStickers",
    "channels.getChannels", "channels.getFullChannel", "channels.getMessages",
    "photos.getUserPhotos", "upload.getFile", "upload.getWebFile", "upload.getCdnFile",
    "langpack.getLangPack", "langpack.getDifference", "langpack.getLanguages",
})
MEDIA_METHODS = frozenset({"upload.getFile", "upload.getWebFile", "upload.getCdnFile"})
BLOCKED_METHODS = frozenset({
    "auth.exportAuthorization", "auth.importAuthorization", "auth.bindTempAuthKey",
    "auth.exportLoginToken", "auth.importLoginToken", "auth.acceptLoginToken",
    "destroy_auth_key", "auth.resetAuthorizations",
})
GHOST_METHODS = frozenset({
    "messages.setTyping", "messages.readHistory", "channels.readHistory",
    "messages.readMessageContents", "channels.readMessageContents",
    "stories.readStories", "stories.incrementStoryViews", "messages.readMentions",
    "messages.readReactions", "messages.readDiscussion",
})


def sensitive(method):
    return method.startswith("auth.") or method in {"account.getPassword", "account.updatePasswordSettings"}


def rpc_error(code, message):
    data = message.encode("ascii")
    if len(data) >= 254:
        raise ValueError("error too long")
    value = bytes([len(data)]) + data
    value += b"\0" * (-len(value) % 4)
    return base64.b64encode(struct.pack("<Ii", 0x2144ca19, code) + value).decode()
