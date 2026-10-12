# Raven wire protocol (version 1)

This is the reference for everything that crosses the network between a **controller**
and a **host**. For the reasoning behind it see [ARCHITECTURE.md](ARCHITECTURE.md).
The implementation lives in `raven/protocol.py`, `raven/host/server.py`,
`raven/host/files.py`, `raven/client/session.py` and `raven/clip.py`.

## Transport

| Property | Value |
|---|---|
| Transport | WebSocket over TLS (`wss://<host>:47470/`) |
| TLS | ≥ 1.2, server certificate self-signed (EC P-256); clients don't verify a CA, they pin the SHA-256 fingerprint of the DER certificate |
| Fingerprint format | 32 bytes, uppercase hex, colon-separated (`AB:CD:…`, 95 characters); UIs show the first 8 bytes |
| Peer filter | The host answers HTTP 403 to non-private peers before the WebSocket upgrade |
| Compression | Disabled (frames are already JPEG and chunks are arbitrary data) |
| Keepalive | WebSocket pings every 20 s (library default) |
| Message size limit (host receives) | 1 MiB before login, 32 MiB after |
| Message size limit (controller receives) | 64 MiB |
| HTTP proxies | Never used, even if the environment configures one |

**Text messages** are JSON objects with a string `type`. Unknown types are ignored, and
unknown fields must be ignored.

**Binary messages** start with a one-byte *kind*:

| Kind | Name | Layout (big-endian) | Direction |
|---|---|---|---|
| `1` | Screen frame | `u8 kind` · `f32 cursor_x` · `f32 cursor_y` · JPEG bytes | host → controller |
| `2` | File chunk | `u8 kind` · `u32 tid` · data bytes (≤ 256 KiB) | both |

`cursor_x` and `cursor_y` are the host pointer's position as 0–1 fractions of the
streamed monitor, or `-1, -1` when the pointer is on another monitor.

## Handshake

```
host → hello        {"type":"hello","version":1,"name":str,"os":"windows|macos|linux",
                     "salt":hex(16 B),"iterations":200000,"nonce":base64(32 B)}
ctrl → auth         {"type":"auth","proof":base64(32 B),"client_name":str}
host → auth_result  {"type":"auth_result","ok":true,"info":{…}}
                  | {"type":"auth_result","ok":false,"error":"Wrong password."}  then close 1008
```

- **Proof:**
  `proof = HMAC-SHA256(key, b"raven-auth-v1|" + nonce + fingerprint_ascii)`, where
  `key = PBKDF2-HMAC-SHA256(password_utf8, salt, iterations, dklen=32)`.
  - `fingerprint_ascii` is the controller's observed certificate fingerprint, in the
    95-character format above.
  - The host verifies the proof against its stored key and **its own** fingerprint, so
    a proof made through a man-in-the-middle certificate fails.
- **Controller validation of `hello`.** The controller refuses a hello, sends nothing,
  and closes the connection if:
  - `version` isn't `1`;
  - `iterations` isn't between 200,000 and 5,000,000;
  - `salt` or `nonce` is shorter than 16 bytes;
  - any field is malformed.

  It removes control and text-direction characters from `name` and caps it at 64
  characters.
- **Refusal before the hello.** Instead of a hello, the host may send
  `{"type":"error","error":str}` and close with code 1008, for example when the address
  is rate-limited or no password is set.
- **Timeouts.** The host waits 30 s for `auth`. A failed login is delayed by 1 s and
  counts against the rate limit: 5 per address and 30 per host per minute, with each
  slot reserved as soon as the connection is accepted.

`info` describes the host and what this controller may do:

```json
{
  "name": "Office PC", "os": "windows", "mac": "a4:83:e7:12:34:56",
  "monitors": [{"index": 1, "width": 1920, "height": 1080}],
  "allow_input": true, "allow_power": true, "allow_files": true,
  "allow_clipboard": true, "clipboard_images": true
}
```

Permissions are checked again on every message, so the host owner can revoke them
mid-session.

## Requests and replies

Any controller message may carry an integer `rid`. The host answers such messages with a
reply carrying the same `rid`:

```
{"type":"reply","rid":7,"ok":true, …payload…}
{"type":"reply","rid":7,"ok":false,"error":"File transfer is turned off on this computer."}
```

- `ping` answers with `pong` (`{"type":"pong","t":…,"rid":7}`).
- `power` answers with `power_result`, also echoing the `rid`.
- Messages without a `rid` get no reply, apart from the unsolicited messages listed
  below.
- The controller's `request()` waits 20 s by default; `fs_upload_end` waits 120 s.

## Controller → host

### Screen and input

| Type | Fields | Notes |
|---|---|---|
| `stream_start` | `monitor` (1–64), `fps` (1–60), `quality` (10–95), `max_width` (320–7680) | Values are clamped. Replaces any running stream. |
| `stream_stop` | | |
| `ack` | | Sent after each frame is received; the host keeps at most 2 frames un-acked |
| `mouse_move` | `x`, `y` (0–1) | Position within the streamed monitor. Needs `allow_input` |
| `mouse_button` | `button` (`left`/`right`/`middle`), `down` (bool) | Needs `allow_input` |
| `scroll` | `dx`, `dy` (wheel notches, −50–50, fractions allowed) | Needs `allow_input` |
| `key` | `key`, `down` (bool) | `key` is one printable character or a name from `platform/inputs.py:SPECIAL_KEYS` (e.g. `enter`, `ctrl`, `cmd`, `f5`). Needs `allow_input` |
| `release_all` | | Releases every key and button this session holds |
| `type_text` | `text` (≤ 10,000 characters, longer text is truncated) | Types the text. Needs `allow_input` |
| `ping` | `t` (optional, echoed back) | Answered with `pong` |

### Power

| Type | Fields | Reply |
|---|---|---|
| `power` | `action`: `sleep` \| `lock` \| `restart` \| `shutdown` | `power_result {action, ok, error?}`. With `ok: true`, the action runs about 1 s later; if it then fails, a second `power_result` with `ok: false` follows. Needs `allow_power` |

### Files

All file requests need `allow_files` and should carry a `rid`. Paths are host-native
strings, exactly as returned by earlier replies.

| Type | Fields | Reply payload |
|---|---|---|
| `fs_places` | | `places: [{name, path}]` (Home, Desktop, Documents, Downloads), `roots: [str]` (drives or `/`, `/Volumes/*`), `downloads: str` |
| `fs_list` | `path` | `path`, `parent` (`""` at a root), `entries: [{name, path, dir, link, size, mtime, hidden}]` (folders first, at most 5,000), `truncated` |
| `fs_mkdir` | `parent`, `name` | `path` (an existing folder is fine) |
| `fs_upload_start` | `dir`, `name`, `size`, `tid` (1 … 2³²−1, unique per session) | `path` (intended destination) |
| *binary kind 2* | `tid` + data | none; data beyond `size` aborts the upload with `fs_error` |
| `fs_upload_end` | `tid`, `sha256` (hex) | `path` (final; may be `name (2).ext`); `ok:false` if the size or hash differs |
| `fs_upload_abort` | `tid` | `{}`; the temp file is deleted |
| `fs_download` | `path`, `tid` | `name`, `size`; then chunks and `fs_done` follow |
| `fs_cancel` | `tid` | `{}`; stops a download |

`name` is always reduced to a single safe component: the last path segment, with no
`..`, Windows-reserved characters or device names, and no invisible or text-direction
characters.

### Clipboard

| Type | Fields | Notes |
|---|---|---|
| `clipboard_sync` | `enabled` (bool) | Reply `{images: bool}`; error if `allow_clipboard` is off or the host has no clipboard backend |
| `clipboard_set` | `kind: "text"`, `text` (≤ 4,194,304 characters) **or** `kind: "image"`, `png` (base64 PNG, ≤ 16 MiB), `digest` | Ignored unless this session enabled sync and `allow_clipboard` is on |

`digest` identifies the content for echo suppression. For text it is the SHA-256 of the
UTF-8 text with `\r\n` normalised (`t:` prefix). For images it is a hash of the pixels
(`i:` prefix).

## Host → controller (unsolicited)

| Type | Fields | When |
|---|---|---|
| `stream_info` | `monitor`, `width`, `height` | A stream starts |
| `cursor` | `x`, `y` | The pointer moved but the screen didn't change |
| `error` | `error` | Capture failed, e.g. no Screen Recording permission |
| `power_result` | `action`, `ok`, `error?` | Reply to `power` (see above) |
| `fs_done` | `tid`, `size`, `sha256` | A download finished; the controller verifies both |
| `fs_error` | `tid`, `error` | A transfer failed |
| `clipboard` | same shape as `clipboard_set` | The host clipboard changed, or another controller set it |

## Closing

| Code | Meaning |
|---|---|
| 1000 | Normal close by either side |
| 1001 | `Disconnected by host` (the host owner pressed *Disconnect all* or turned remote access off) |
| 1008 | Refused: wrong password, rate-limited, or not set up (the reason is in the close frame) |

On close, the host releases held keys and buttons, stops the stream, aborts uploads
(deleting their temp files), cancels downloads and leaves clipboard sync.

## mDNS advertisement

| Field | Value |
|---|---|
| Service type | `_raven._tcp.local.` (IPv4) |
| Instance name | the host's display name (dots removed, ≤ 50 characters; renamed on conflict) |
| Port | the host's listening port |
| TXT `name` | display name |
| TXT `os` | `windows` / `macos` / `linux` |
| TXT `fp` | certificate fingerprint |
| TXT `v` | protocol version (`1`) |

Advertisements are unauthenticated. Controllers use them only to list devices and as
candidate addresses that must prove the pinned fingerprint over TLS.

## Wake-on-LAN

UDP datagram `FF×6 + MAC×16` (102 bytes), sent to `255.255.255.255` and to the subnet's
directed broadcast, on ports 9 and 7.

## Versioning

`PROTOCOL_VERSION` (`raven/__init__.py`) is sent in `hello`; controllers refuse other
versions. Adding new message types or fields is backwards-compatible, because unknown
ones are ignored. Changing the meaning of existing fields requires a version bump.
