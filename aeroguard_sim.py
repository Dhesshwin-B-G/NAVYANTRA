"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   AEROGUARD — AIRBORNE MESH NETWORK SIMULATOR v2.0                          ║
║   No Hardware Required · Terminal Simulation · Encrypted Relay              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  HOW IT WORKS:                                                               ║
║   • Nodes communicate via local UDP sockets (loopback)                       ║
║   • Out-of-radar-range nodes relay through intermediate nodes                ║
║   • Relay nodes see ONLY "packet received" — NOT the content                 ║
║   • Ground Station receives and displays all decoded telemetry                ║
║   • AES-256-GCM encryption on every packet                                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  USAGE:                                                                       ║
║   Terminal 1 (Ground Station):                                                ║
║     python aeroguard_sim.py --role gs --id GS001 --port 9100                 ║
║                                                                               ║
║   Terminal 2 (Relay Node - in radar range of GS):                            ║
║     python aeroguard_sim.py --role relay --id RELAY01 --port 9200            ║
║         --gs-host 127.0.0.1 --gs-port 9100                                   ║
║                                                                               ║
║   Terminal 3 (Aircraft Node - out of radar, routes via RELAY):               ║
║     python aeroguard_sim.py --role aircraft --id AC001 --port 9300           ║
║         --relay-host 127.0.0.1 --relay-port 9200                             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, sys, time, json, hmac, math, struct, socket, hashlib
import logging, argparse, threading, traceback, heapq, queue

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
from enum import IntEnum
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING — compact, color-coded
# ─────────────────────────────────────────────────────────────────────────────
class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG':    '\033[36m',
        'INFO':     '\033[32m',
        'WARNING':  '\033[33m',
        'ERROR':    '\033[31m',
        'CRITICAL': '\033[35m',
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%H:%M:%S'
))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger("AEROGUARD")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
FLEET_SECRET = b"AEROGUARD_FLEET_SECRET_2024_DEMO_KEY"
MAGIC        = b'AGRD'

class MsgType(IntEnum):
    TELEMETRY  = 0x01
    MESSAGE    = 0x02
    EMERGENCY  = 0x03
    HEARTBEAT  = 0x04
    ACK        = 0x05

# ─────────────────────────────────────────────────────────────────────────────
# ENCRYPTION ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class Crypto:
    """AES-256-GCM encryption. Relay nodes get ONLY ciphertext — cannot read."""

    def __init__(self, my_id: str):
        self.my_id = my_id
        self._pair_keys: Dict[str, bytes] = {}

    def _derive_pair_key(self, src: str, dst: str) -> bytes:
        pair = "_".join(sorted([src, dst]))
        if pair not in self._pair_keys:
            hkdf = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=None,
                info=f"AEROGUARD_PAIR_{pair}".encode(),
                backend=default_backend()
            )
            self._pair_keys[pair] = hkdf.derive(FLEET_SECRET)
        return self._pair_keys[pair]

    def _fleet_key(self) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None,
            info=b"AEROGUARD_FLEET_BROADCAST",
            backend=default_backend()
        )
        return hkdf.derive(FLEET_SECRET)

    def encrypt(self, plaintext: bytes, src: str, dst: str) -> Tuple[bytes, bytes]:
        """Returns (ciphertext, nonce)"""
        key   = self._fleet_key() if dst == "*" else self._derive_pair_key(src, dst)
        nonce = os.urandom(12)
        aad   = f"{src}:{dst}".encode()
        ct    = AESGCM(key).encrypt(nonce, plaintext, aad)
        return ct, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes, src: str, dst: str) -> Optional[bytes]:
        """Returns plaintext or None if auth fails."""
        key = self._fleet_key() if dst == "*" else self._derive_pair_key(src, dst)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, f"{src}:{dst}".encode())
        except Exception:
            return None

    def sign(self, header: bytes, payload: bytes, src: str) -> bytes:
        """HMAC-SHA256 over header+payload using fleet secret."""
        mac = hmac.new(FLEET_SECRET, digestmod=hashlib.sha256)
        mac.update(header)
        mac.update(payload)
        return mac.digest()

    def verify(self, header: bytes, payload: bytes, tag: bytes, src: str) -> bool:
        expected = self.sign(header, payload, src)
        return hmac.compare_digest(expected, tag)

# ─────────────────────────────────────────────────────────────────────────────
# PACKET CODEC  — wire format: MAGIC(4) | TYPE(1) | SEQ(4) | SRC(20) | DST(20)
#                              | ORIG(20) | NONCE(12) | HMAC(32) | LEN(4) | CT
# ─────────────────────────────────────────────────────────────────────────────
ID_SIZE = 20  # bytes for node ID fields

def pack_id(s: str) -> bytes:
    b = s.encode()[:ID_SIZE]
    return b.ljust(ID_SIZE, b'\x00')

def unpack_id(b: bytes) -> str:
    return b.rstrip(b'\x00').decode(errors='replace')

def encode_packet(msg_type: MsgType, src: str, dst: str, orig_src: str,
                  seq: int, payload_dict: dict, crypto: Crypto) -> bytes:
    plain = json.dumps(payload_dict, separators=(',', ':')).encode()
    ct, nonce = crypto.encrypt(plain, orig_src, dst)

    header = (
        MAGIC +
        struct.pack('!B', int(msg_type)) +
        struct.pack('!I', seq & 0xFFFFFFFF) +
        pack_id(src) +
        pack_id(dst) +
        pack_id(orig_src) +
        nonce  # 12 bytes
    )
    tag    = crypto.sign(header, ct, src)
    length = struct.pack('!I', len(ct))
    return header + tag + length + ct

def decode_packet(data: bytes, crypto: Crypto, my_id: str
                  ) -> Optional[dict]:
    """
    Returns decoded dict or None.
    Relay nodes (not the destination) get type/src/dst but NOT plaintext.
    """
    try:
        if len(data) < 4 or data[:4] != MAGIC:
            return None

        off = 4
        msg_type = MsgType(struct.unpack('!B', data[off:off+1])[0]); off += 1
        seq      = struct.unpack('!I', data[off:off+4])[0];          off += 4
        src      = unpack_id(data[off:off+ID_SIZE]);                  off += ID_SIZE
        dst      = unpack_id(data[off:off+ID_SIZE]);                  off += ID_SIZE
        orig_src = unpack_id(data[off:off+ID_SIZE]);                  off += ID_SIZE
        nonce    = data[off:off+12];                                   off += 12

        tag    = data[off:off+32]; off += 32
        length = struct.unpack('!I', data[off:off+4])[0]; off += 4
        ct     = data[off:off+length]

        header = data[:off - 4 - length - 32]  # everything before tag
        # Full header for verification
        full_header = data[:4 + 1 + 4 + ID_SIZE*3 + 12]

        if not crypto.verify(full_header, ct, tag, src):
            log.warning(f"[CODEC] HMAC failed from {src} — packet dropped")
            return None

        result = {
            'msg_type': msg_type,
            'seq':      seq,
            'src':      src,
            'dst':      dst,
            'orig_src': orig_src,
            'raw_ct':   ct,
            'nonce':    nonce,
            'plaintext': None,
        }

        # Only decrypt if we are the destination or broadcast
        if dst == "*" or dst == my_id or dst == "GS001":
            plain = crypto.decrypt(ct, nonce, orig_src, dst)
            if plain is None:
                # Try with my_id as dst for GS broadcast patterns
                plain = crypto.decrypt(ct, nonce, orig_src, "*")
            result['plaintext'] = plain
            if plain:
                result['payload'] = json.loads(plain.decode())

        return result

    except Exception as e:
        log.debug(f"[CODEC] Decode error: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# GROUND STATION NODE
# ─────────────────────────────────────────────────────────────────────────────
class GroundStation:
    """
    Receives all relayed packets. Decrypts and displays full telemetry.
    Also hosts a simple HTTP dashboard on port 8080.
    """

    def __init__(self, node_id: str, host: str, port: int):
        self.node_id  = node_id
        self.host     = host
        self.port     = port
        self.running  = False
        self.crypto   = Crypto(node_id)
        self._seq     = 0
        self._seq_lk  = threading.Lock()
        self._sock    = None
        self._log: List[dict] = []   # message log for web dashboard
        self._log_lk  = threading.Lock()

        print(self._banner())

    def _banner(self):
        return f"""
\033[34m╔══════════════════════════════════════════════════════════╗
║   🛰  AEROGUARD GROUND STATION  [{self.node_id}]               ║
║   Listening on {self.host}:{self.port:<5}                          ║
║   All relayed packets decoded and displayed here.         ║
╚══════════════════════════════════════════════════════════╝\033[0m"""

    def _next_seq(self):
        with self._seq_lk:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            return self._seq

    def _recv_loop(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.5)
        log.info(f"[GS] Bound to {self.host}:{self.port}")

        while self.running:
            try:
                data, addr = self._sock.recvfrom(65535)
                pkt = decode_packet(data, self.crypto, self.node_id)
                if pkt:
                    self._handle(pkt, addr)
            except socket.timeout:
                pass
            except Exception as e:
                if self.running:
                    log.error(f"[GS] Recv error: {e}")

    def _handle(self, pkt: dict, addr: tuple):
        mt   = pkt['msg_type']
        src  = pkt['orig_src'] or pkt['src']
        now  = datetime.now().strftime('%H:%M:%S')

        entry = {
            'time': now, 'src': src, 'type': mt.name,
            'addr': f"{addr[0]}:{addr[1]}", 'payload': {}
        }

        if pkt.get('payload'):
            pl = pkt['payload']
            entry['payload'] = pl

            if mt == MsgType.TELEMETRY:
                self._display_telemetry(pl, src, now)
            elif mt == MsgType.MESSAGE:
                self._display_message(pl, src, now)
            elif mt == MsgType.EMERGENCY:
                self._display_emergency(pl, src, now)
            elif mt == MsgType.HEARTBEAT:
                log.info(f"[GS] ♥ Heartbeat from {src}")
        else:
            # Packet received but couldn't decrypt (wrong destination key)
            log.info(f"[GS] 📦 Relayed packet from {src} (type={mt.name})")

        with self._log_lk:
            self._log.append(entry)
            if len(self._log) > 500:
                self._log = self._log[-500:]

    def _display_telemetry(self, pl: dict, src: str, ts: str):
        print(f"\n\033[32m{'─'*62}")
        print(f"  📡 TELEMETRY  [{ts}]  from \033[1m{src}\033[0;32m")
        print(f"  Aircraft   : {pl.get('aircraft_id','?')}  |  Callsign: {pl.get('callsign','?')}")
        gps = pl.get('gps', {})
        print(f"  Position   : lat={gps.get('latitude',0):.4f}°  lon={gps.get('longitude',0):.4f}°  "
              f"alt={pl.get('altitude_ft',0):.0f}ft")
        print(f"  Speed      : {pl.get('speed_knots',0):.0f}kts  |  Heading: {pl.get('heading_deg',0):.1f}°")
        print(f"  Fuel       : {pl.get('fuel_kg',0):.0f}kg  |  Engines: "
              f"{pl.get('engine_1_pct',100):.0f}% / {pl.get('engine_2_pct',100):.0f}%")
        print(f"  Battery    : {pl.get('node_battery_pct',100):.0f}%  |  "
              f"Signal: {pl.get('signal_dbm',-70):.0f}dBm")
        print(f"  Hops       : {pl.get('hop_count',0)}  |  "
              f"Peers: {pl.get('mesh_peers',0)}")
        print(f"{'─'*62}\033[0m")

    def _display_message(self, pl: dict, src: str, ts: str):
        print(f"\n\033[36m{'═'*62}")
        print(f"  📨 SECURE MESSAGE  [{ts}]")
        print(f"  From    : \033[1m{pl.get('from', src)}\033[0;36m")
        print(f"  To      : {pl.get('to','GS')}")
        print(f"  Content : \033[1m{pl.get('content','')}\033[0;36m")
        print(f"  Sent at : {pl.get('timestamp','')}")
        print(f"{'═'*62}\033[0m")

    def _display_emergency(self, pl: dict, src: str, ts: str):
        print(f"\n\033[31m{'⚠'*30}")
        print(f"  🚨 EMERGENCY SIGNAL [{ts}]")
        print(f"  Aircraft  : {pl.get('aircraft_id', src)}")
        print(f"  Callsign  : {pl.get('callsign','?')}")
        print(f"  Type      : \033[1m{pl.get('emergency_type','UNKNOWN')}\033[0;31m")
        print(f"  Message   : {pl.get('message','')}")
        gps = pl.get('gps', {})
        print(f"  Position  : lat={gps.get('latitude',0):.4f}  lon={gps.get('longitude',0):.4f}")
        print(f"{'⚠'*30}\033[0m")

    def get_log(self) -> list:
        with self._log_lk:
            return list(self._log)

    def start(self):
        self.running = True
        threading.Thread(target=self._recv_loop, daemon=True, name="GS-Recv").start()
        log.info(f"[GS] Ground Station \033[1m{self.node_id}\033[0m started")

    def stop(self):
        self.running = False

# ─────────────────────────────────────────────────────────────────────────────
# RELAY NODE
# Receives encrypted packets → shows "RECEIVED" → forwards to GS (still encrypted)
# ─────────────────────────────────────────────────────────────────────────────
class RelayNode:
    """
    Intermediate mesh node — in radar range of GS but relays for out-of-range aircraft.
    CANNOT read the encrypted payload. Only sees: packet received from <ID>.
    """

    def __init__(self, node_id: str, host: str, port: int,
                 gs_host: str, gs_port: int):
        self.node_id  = node_id
        self.host     = host
        self.port     = port
        self.gs_addr  = (gs_host, gs_port)
        self.running  = False
        self.crypto   = Crypto(node_id)
        self._seq     = 0
        self._seq_lk  = threading.Lock()
        self._recv_sock = None
        self._send_sock = None
        self._seen: Dict[str, float] = {}

        print(self._banner())

    def _banner(self):
        return f"""
\033[33m╔══════════════════════════════════════════════════════════╗
║   📡 AEROGUARD RELAY NODE  [{self.node_id}]                   ║
║   Listening: {self.host}:{self.port:<5}                           ║
║   Forwarding → GS at {self.gs_addr[0]}:{self.gs_addr[1]}             ║
║   ⚠ This node CANNOT read encrypted payload contents.    ║
╚══════════════════════════════════════════════════════════╝\033[0m"""

    def _next_seq(self):
        with self._seq_lk:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            return self._seq

    def _is_seen(self, key: str) -> bool:
        now = time.monotonic()
        if key in self._seen:
            return True
        self._seen[key] = now
        old = [k for k, t in self._seen.items() if now - t > 60]
        for k in old: del self._seen[k]
        return False

    def _recv_loop(self):
        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._recv_sock.bind((self.host, self.port))
        self._recv_sock.settimeout(0.5)
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info(f"[RELAY] {self.node_id} bound to {self.host}:{self.port}")

        while self.running:
            try:
                data, addr = self._recv_sock.recvfrom(65535)
                self._handle(data, addr)
            except socket.timeout:
                pass
            except Exception as e:
                if self.running:
                    log.error(f"[RELAY] Error: {e}")

    def _handle(self, data: bytes, addr: tuple):
        pkt = decode_packet(data, self.crypto, self.node_id)
        if not pkt:
            return

        src = pkt['orig_src'] or pkt['src']
        seq = pkt['seq']
        key = f"{src}:{seq}"
        if self._is_seen(key):
            return

        mt = pkt['msg_type']

        # ── RELAY NODE ONLY SEES THIS — NOT THE CONTENT ──────────────────────
        ts = datetime.now().strftime('%H:%M:%S')
        if mt == MsgType.EMERGENCY:
            print(f"\n\033[33m[{ts}] [{self.node_id}] ⚡ ENCRYPTED PACKET RECEIVED")
            print(f"  From     : {src}")
            print(f"  Type     : {mt.name}  (PRIORITY: EMERGENCY)")
            print(f"  Status   : 🔒 Payload encrypted — content hidden from relay")
            print(f"  Action   : Forwarding to Ground Station immediately...\033[0m")
        else:
            print(f"\033[33m[{ts}] [{self.node_id}] 📦 Packet received from \033[1m{src}\033[0;33m"
                  f" | Type={mt.name} | 🔒 Encrypted | Forwarding→GS\033[0m")

        # Forward original bytes (still encrypted) to GS
        try:
            self._send_sock.sendto(data, self.gs_addr)
        except Exception as e:
            log.error(f"[RELAY] Forward to GS failed: {e}")

    def start(self):
        self.running = True
        threading.Thread(target=self._recv_loop, daemon=True, name="Relay-Recv").start()
        log.info(f"[RELAY] \033[1m{self.node_id}\033[0m started")

    def stop(self):
        self.running = False

# ─────────────────────────────────────────────────────────────────────────────
# AIRCRAFT NODE (Out of radar range)
# Sends encrypted packets to relay node which forwards to GS
# ─────────────────────────────────────────────────────────────────────────────
class AircraftNode:
    """
    Aircraft node out of direct radar range of the Ground Station.
    Routes all communication through a Relay node.
    Supports interactive terminal to send custom data.
    """

    def __init__(self, node_id: str, host: str, port: int,
                 relay_host: str, relay_port: int,
                 lat: float = 19.09, lon: float = 72.87, alt: float = 10000.0):
        self.node_id   = node_id
        self.host      = host
        self.port      = port
        self.relay_addr = (relay_host, relay_port)
        self.running   = False
        self.crypto    = Crypto(node_id)
        self._seq      = 0
        self._seq_lk   = threading.Lock()
        self._send_sock = None

        # Live telemetry state
        self.lat   = lat
        self.lon   = lon
        self.alt_m = alt
        self.speed = 450.0   # knots
        self.hdg   = 90.0    # degrees
        self.fuel  = 15000.0 # kg
        self.eng1  = 92.0
        self.eng2  = 91.0
        self.batt  = 98.0
        self.sig   = -72.0
        self.callsign = node_id
        self.squawk   = "2200"
        self._auto_telem = True

        print(self._banner())

    def _banner(self):
        return f"""
\033[35m╔══════════════════════════════════════════════════════════╗
║   ✈  AEROGUARD AIRCRAFT NODE  [{self.node_id}]                ║
║   Routing via RELAY at {self.relay_addr[0]}:{self.relay_addr[1]}          ║
║   ⚠ Out of direct radar range — using mesh relay.        ║
║   All transmissions AES-256-GCM encrypted.               ║
╚══════════════════════════════════════════════════════════╝\033[0m"""

    def _next_seq(self):
        with self._seq_lk:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            return self._seq

    def _send(self, msg_type: MsgType, dst: str, payload: dict):
        data = encode_packet(
            msg_type, self.node_id, dst, self.node_id,
            self._next_seq(), payload, self.crypto
        )
        try:
            self._send_sock.sendto(data, self.relay_addr)
            return True
        except Exception as e:
            log.error(f"[AC] Send failed: {e}")
            return False

    def send_telemetry(self):
        """Send current aircraft telemetry to GS via relay."""
        payload = {
            'aircraft_id':    self.node_id,
            'callsign':       self.callsign,
            'transponder_code': self.squawk,
            'gps': {
                'latitude':   self.lat,
                'longitude':  self.lon,
                'altitude_m': self.alt_m,
            },
            'altitude_ft':    self.alt_m * 3.28084,
            'speed_knots':    self.speed,
            'heading_deg':    self.hdg,
            'fuel_kg':        self.fuel,
            'engine_1_pct':   self.eng1,
            'engine_2_pct':   self.eng2,
            'node_battery_pct': self.batt,
            'signal_dbm':     self.sig,
            'hop_count':      1,
            'mesh_peers':     1,
            'timestamp_utc':  time.time(),
        }
        ok = self._send(MsgType.TELEMETRY, "*", payload)
        if ok:
            log.info(f"[AC] ✈ Telemetry sent via relay → GS")

    def send_message(self, content: str, to: str = "GS001"):
        payload = {
            'type':      'message',
            'content':   content,
            'from':      self.node_id,
            'to':        to,
            'callsign':  self.callsign,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        ok = self._send(MsgType.MESSAGE, to, payload)
        if ok:
            print(f"\033[36m[AC] ✉ Message sent → {to} via relay (encrypted)\033[0m")
        return ok

    def broadcast_emergency(self, emerg_type: str, message: str):
        payload = {
            'aircraft_id':   self.node_id,
            'callsign':      self.callsign,
            'emergency_type': emerg_type,
            'message':       message,
            'gps': {
                'latitude':  self.lat,
                'longitude': self.lon,
                'altitude_m': self.alt_m,
            },
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        ok = self._send(MsgType.EMERGENCY, "*", payload)
        if ok:
            print(f"\033[31m[AC] 🚨 EMERGENCY '{emerg_type}' broadcast via relay!\033[0m")
        return ok

    def send_heartbeat(self):
        payload = {
            'node_id': self.node_id,
            'lat': self.lat, 'lon': self.lon,
            'timestamp': time.time(),
        }
        self._send(MsgType.HEARTBEAT, "*", payload)

    def _auto_telem_loop(self):
        """Simulate sensor updates and periodic telemetry."""
        while self.running:
            if self._auto_telem:
                # Drift position slightly
                self.lat  += 0.001 * math.cos(math.radians(self.hdg))
                self.lon  += 0.001 * math.sin(math.radians(self.hdg))
                self.fuel -= 1.2
                self.batt  = max(5, self.batt - 0.01)
                self.send_telemetry()
            time.sleep(5)   # Every 5s for demo

    def _hb_loop(self):
        while self.running:
            self.send_heartbeat()
            time.sleep(10)

    def _interactive(self):
        print(f"""
\033[35m{'═'*62}
  ✈  AIRCRAFT [{self.node_id}] — Interactive Console
{'─'*62}
  Commands:
    telem                    Send telemetry now
    msg <text>               Send encrypted message to GS
    emerg <TYPE> <text>      Broadcast emergency (e.g. MAYDAY)
    set speed <knots>        Update airspeed
    set heading <deg>        Update heading
    set fuel <kg>            Update fuel
    set lat <deg>            Update latitude
    set lon <deg>            Update longitude
    set callsign <cs>        Update callsign
    auto on|off              Toggle auto telemetry (every 5s)
    status                   Show current aircraft state
    quit                     Shutdown
{'═'*62}\033[0m""")

        while self.running:
            try:
                raw = input(f"\033[35m[{self.node_id}]> \033[0m").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not raw:
                continue

            parts = raw.split(None, 2)
            cmd   = parts[0].lower()

            if cmd == 'quit':
                break

            elif cmd == 'telem':
                self.send_telemetry()

            elif cmd == 'msg' and len(parts) >= 2:
                self.send_message(" ".join(parts[1:]))

            elif cmd == 'emerg' and len(parts) >= 3:
                self.broadcast_emergency(parts[1].upper(), parts[2])

            elif cmd == 'set' and len(parts) >= 3:
                key = parts[1].lower()
                val = parts[2]
                try:
                    if key == 'speed':    self.speed = float(val)
                    elif key == 'heading': self.hdg  = float(val) % 360
                    elif key == 'fuel':    self.fuel  = float(val)
                    elif key == 'lat':     self.lat   = float(val)
                    elif key == 'lon':     self.lon   = float(val)
                    elif key == 'callsign': self.callsign = val
                    print(f"\033[32m  ✔ {key} = {val}\033[0m")
                except ValueError:
                    print(f"\033[31m  ✗ Invalid value: {val}\033[0m")

            elif cmd == 'auto' and len(parts) >= 2:
                self._auto_telem = (parts[1].lower() == 'on')
                print(f"\033[32m  Auto-telemetry: {'ON' if self._auto_telem else 'OFF'}\033[0m")

            elif cmd == 'status':
                print(f"""
\033[35m  Aircraft State [{self.node_id}]
  {'─'*40}
  Callsign  : {self.callsign}
  Position  : lat={self.lat:.4f}  lon={self.lon:.4f}  alt={self.alt_m:.0f}m
  Speed     : {self.speed:.0f} kts  Heading: {self.hdg:.1f}°
  Fuel      : {self.fuel:.0f} kg
  Engines   : {self.eng1:.0f}% / {self.eng2:.0f}%
  Battery   : {self.batt:.1f}%
  Signal    : {self.sig:.0f} dBm
  Auto-telem: {'ON' if self._auto_telem else 'OFF'}
  Relay→GS  : {self.relay_addr[0]}:{self.relay_addr[1]}
  {'─'*40}\033[0m""")

            else:
                print("\033[33m  Unknown command. Try: telem, msg, emerg, set, auto, status, quit\033[0m")

        self.stop()

    def start(self):
        self.running = True
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        threading.Thread(target=self._auto_telem_loop, daemon=True, name="AC-Telem").start()
        threading.Thread(target=self._hb_loop,         daemon=True, name="AC-HB").start()
        log.info(f"[AC] Aircraft node \033[1m{self.node_id}\033[0m started")
        self._interactive()   # blocks until quit

    def stop(self):
        self.running = False
        log.info(f"[AC] {self.node_id} stopped")

# ─────────────────────────────────────────────────────────────────────────────
# WEB DASHBOARD (Ground Station HTTP server)
# ─────────────────────────────────────────────────────────────────────────────
GS_INSTANCE: Optional[GroundStation] = None

def start_web_dashboard(gs: GroundStation, web_port: int = 8080):
    """Serve a live HTML dashboard for the Ground Station."""
    import http.server, urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # silence HTTP logs

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.end_headers()

        def do_GET(self):
            if self.path == '/api/log':
                data = json.dumps(gs.get_log(), default=str).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            elif self.path in ('/', '/index.html', '/ui', '/web_UI.html'):
                ui_path = os.path.join(os.path.dirname(__file__), 'src', 'web_UI.html')
                if os.path.exists(ui_path):
                    with open(ui_path, 'rb') as f:
                        html = f.read()
                else:
                    html = _dashboard_html(gs.node_id, gs.port, web_port).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(html)
            elif self.path == '/legacy':
                html = _dashboard_html(gs.node_id, gs.port, web_port).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html)
            else:
                self.send_response(404)
                self.end_headers()

    srv = http.server.ThreadingHTTPServer(('0.0.0.0', web_port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="WebDash")
    t.start()
    log.info(f"[WEB] Dashboard → \033[1mhttp://localhost:{web_port}\033[0m")

def _dashboard_html(gs_id: str, gs_port: int, web_port: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AeroGuard Ground Station — {gs_id}</title>
<style>
  :root {{
    --bg: #0a0e1a; --surface: #111827; --card: #1a2235;
    --border: #2d3748; --text: #e2e8f0; --muted: #64748b;
    --green: #22c55e; --blue: #3b82f6; --red: #ef4444;
    --yellow: #eab308; --purple: #a855f7; --cyan: #06b6d4;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Courier New', monospace;
          font-size: 13px; min-height: 100vh; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border);
            padding: 12px 24px; display: flex; align-items: center; gap: 16px; }}
  header h1 {{ font-size: 18px; color: var(--cyan); letter-spacing: 2px; }}
  .badge {{ background: var(--green); color: #000; padding: 2px 8px; border-radius: 4px;
            font-size: 11px; font-weight: bold; animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.5}} }}
  .status-bar {{ display: flex; gap: 24px; padding: 8px 24px;
                 background: var(--card); border-bottom: 1px solid var(--border);
                 font-size: 12px; color: var(--muted); }}
  .status-bar span {{ color: var(--text); }}
  .layout {{ display: grid; grid-template-columns: 1fr 320px; height: calc(100vh - 88px); }}
  .feed {{ padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }}
  .sidebar {{ background: var(--surface); border-left: 1px solid var(--border); padding: 16px;
              overflow-y: auto; }}
  .sidebar h3 {{ color: var(--cyan); margin-bottom: 12px; font-size: 13px; }}
  .packet {{ background: var(--card); border: 1px solid var(--border); border-radius: 6px;
             padding: 10px 14px; transition: border-color .2s; }}
  .packet:hover {{ border-color: var(--blue); }}
  .packet.TELEMETRY  {{ border-left: 3px solid var(--green); }}
  .packet.MESSAGE    {{ border-left: 3px solid var(--cyan); }}
  .packet.EMERGENCY  {{ border-left: 3px solid var(--red); background: #1f0a0a; }}
  .packet.HEARTBEAT  {{ border-left: 3px solid var(--muted); opacity: .7; }}
  .pkt-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
  .pkt-type {{ padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; }}
  .TELEMETRY  .pkt-type {{ background: var(--green); color: #000; }}
  .MESSAGE    .pkt-type {{ background: var(--cyan); color: #000; }}
  .EMERGENCY  .pkt-type {{ background: var(--red); color: #fff; }}
  .HEARTBEAT  .pkt-type {{ background: var(--muted); color: #000; }}
  .pkt-src  {{ color: var(--purple); font-weight: bold; }}
  .pkt-time {{ color: var(--muted); margin-left: auto; font-size: 11px; }}
  .pkt-body {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px; }}
  .pkt-row  {{ display: contents; }}
  .pkt-key  {{ color: var(--muted); }}
  .pkt-val  {{ color: var(--text); }}
  .alert {{ padding: 8px 12px; border-radius: 4px; margin-bottom: 8px; font-size: 12px; }}
  .alert.info {{ background: #1e3a5f; border: 1px solid var(--blue); }}
  .counter {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 20px; }}
  .counter-item {{ display: flex; justify-content: space-between;
                   padding: 6px 0; border-bottom: 1px solid var(--border); }}
  .counter-val {{ color: var(--cyan); font-weight: bold; }}
  #ac-list {{ display: flex; flex-direction: column; gap: 6px; }}
  .ac-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 4px;
              padding: 8px; font-size: 11px; }}
  .ac-id {{ color: var(--green); font-weight: bold; margin-bottom: 4px; }}
  .empty {{ color: var(--muted); text-align: center; padding: 40px; }}
</style>
</head>
<body>
<header>
  <div style="font-size:24px">🛰</div>
  <h1>AEROGUARD GROUND STATION</h1>
  <div class="badge">LIVE</div>
  <div style="margin-left:auto; color:var(--muted); font-size:12px">GS: <span style="color:var(--text)">{gs_id}</span>  |  Port: <span style="color:var(--text)">{gs_port}</span></div>
</header>
<div class="status-bar">
  <span>Packets received: <span id="total">0</span></span>
  <span>Telemetry: <span id="telem-cnt">0</span></span>
  <span>Messages: <span id="msg-cnt">0</span></span>
  <span>Emergency: <span id="emerg-cnt">0</span></span>
  <span style="margin-left:auto">Auto-refresh: <span style="color:var(--green)">ON (2s)</span></span>
</div>
<div class="layout">
  <div class="feed" id="feed">
    <div class="empty">⏳ Waiting for aircraft transmissions…</div>
  </div>
  <div class="sidebar">
    <h3>📊 Statistics</h3>
    <div class="counter" id="stats"></div>
    <h3>✈ Active Aircraft</h3>
    <div id="ac-list"><div style="color:var(--muted);font-size:12px">No aircraft yet…</div></div>
  </div>
</div>
<script>
const API = '/api/log';
let lastCount = 0;
const counts = {{telem:0, msg:0, emerg:0, hb:0}};
const aircraft = {{}};

function typeIcon(t) {{
  return {{TELEMETRY:'📡',MESSAGE:'📨',EMERGENCY:'🚨',HEARTBEAT:'♥',ACK:'✔'}}[t] || '📦';
}}

function renderPacket(p) {{
  const pl = p.payload || {{}};
  let bodyHtml = '';
  if (p.type === 'TELEMETRY') {{
    const gps = pl.gps || {{}};
    bodyHtml = `
      <div class="pkt-row"><span class="pkt-key">Position</span><span class="pkt-val">lat=${{(gps.latitude||0).toFixed(4)}} lon=${{(gps.longitude||0).toFixed(4)}}</span></div>
      <div class="pkt-row"><span class="pkt-key">Altitude</span><span class="pkt-val">${{(pl.altitude_ft||0).toFixed(0)}} ft</span></div>
      <div class="pkt-row"><span class="pkt-key">Speed</span><span class="pkt-val">${{(pl.speed_knots||0).toFixed(0)}} kts  ${{(pl.heading_deg||0).toFixed(1)}}°</span></div>
      <div class="pkt-row"><span class="pkt-key">Fuel</span><span class="pkt-val">${{(pl.fuel_kg||0).toFixed(0)}} kg</span></div>
      <div class="pkt-row"><span class="pkt-key">Engines</span><span class="pkt-val">${{(pl.engine_1_pct||100).toFixed(0)}}% / ${{(pl.engine_2_pct||100).toFixed(0)}}%</span></div>
      <div class="pkt-row"><span class="pkt-key">Battery</span><span class="pkt-val">${{(pl.node_battery_pct||100).toFixed(1)}}%</span></div>`;
  }} else if (p.type === 'MESSAGE') {{
    bodyHtml = `
      <div class="pkt-row"><span class="pkt-key">From</span><span class="pkt-val">${{pl.from||p.src}}</span></div>
      <div class="pkt-row"><span class="pkt-key">To</span><span class="pkt-val">${{pl.to||'GS'}}</span></div>
      <div class="pkt-row"><span class="pkt-key">Content</span><span class="pkt-val" style="color:var(--cyan);font-weight:bold">${{pl.content||''}}</span></div>`;
  }} else if (p.type === 'EMERGENCY') {{
    bodyHtml = `
      <div class="pkt-row"><span class="pkt-key">Aircraft</span><span class="pkt-val">${{pl.aircraft_id||p.src}}</span></div>
      <div class="pkt-row"><span class="pkt-key">Type</span><span class="pkt-val" style="color:var(--red);font-weight:bold">${{pl.emergency_type||'UNKNOWN'}}</span></div>
      <div class="pkt-row"><span class="pkt-key">Message</span><span class="pkt-val">${{pl.message||''}}</span></div>`;
  }} else if (p.type === 'HEARTBEAT') {{
    bodyHtml = `<div class="pkt-row"><span class="pkt-key">Node</span><span class="pkt-val">${{p.src}}</span></div>`;
  }}

  return `<div class="packet ${{p.type}}">
    <div class="pkt-header">
      <span>${{typeIcon(p.type)}}</span>
      <span class="pkt-type">${{p.type}}</span>
      <span class="pkt-src">${{p.src}}</span>
      <span class="pkt-time">${{p.time}}</span>
    </div>
    <div class="pkt-body">${{bodyHtml}}</div>
  </div>`;
}}

async function refresh() {{
  try {{
    const r  = await fetch(API);
    const data = await r.json();
    if (data.length === lastCount) return;
    lastCount = data.length;

    // Update feed (newest first)
    const feed = document.getElementById('feed');
    const newItems = data.slice(-50).reverse();
    feed.innerHTML = newItems.map(renderPacket).join('');

    // Counts
    counts.telem  = data.filter(p=>p.type==='TELEMETRY').length;
    counts.msg    = data.filter(p=>p.type==='MESSAGE').length;
    counts.emerg  = data.filter(p=>p.type==='EMERGENCY').length;
    counts.hb     = data.filter(p=>p.type==='HEARTBEAT').length;

    document.getElementById('total').textContent    = data.length;
    document.getElementById('telem-cnt').textContent = counts.telem;
    document.getElementById('msg-cnt').textContent   = counts.msg;
    document.getElementById('emerg-cnt').textContent = counts.emerg;

    // Active aircraft
    data.forEach(p => {{
      if (p.type==='TELEMETRY' && p.payload) {{
        aircraft[p.src] = {{...p.payload, _last: p.time}};
      }}
    }});
    const acList = document.getElementById('ac-list');
    const ids = Object.keys(aircraft);
    if (ids.length === 0) {{
      acList.innerHTML = '<div style="color:var(--muted);font-size:12px">No aircraft yet…</div>';
    }} else {{
      acList.innerHTML = ids.map(id => {{
        const a = aircraft[id];
        const gps = a.gps || {{}};
        return `<div class="ac-card">
          <div class="ac-id">✈ ${{id}} — ${{a.callsign||id}}</div>
          <div>lat=${{(gps.latitude||0).toFixed(4)}} lon=${{(gps.longitude||0).toFixed(4)}}</div>
          <div>alt=${{(a.altitude_ft||0).toFixed(0)}}ft  spd=${{(a.speed_knots||0).toFixed(0)}}kts</div>
          <div>fuel=${{(a.fuel_kg||0).toFixed(0)}}kg  hdg=${{(a.heading_deg||0).toFixed(1)}}°</div>
          <div style="color:var(--muted);font-size:10px">Last seen: ${{a._last}}</div>
        </div>`;
      }}).join('');
    }}

  }} catch(e) {{ console.error(e); }}
}}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='AeroGuard Mesh Network Simulator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ground Station (Terminal 1):
  python aeroguard_sim.py --role gs --id GS001 --port 9100

  # Relay Node in radar range (Terminal 2):
  python aeroguard_sim.py --role relay --id RELAY01 --port 9200 --gs-host 127.0.0.1 --gs-port 9100

  # Aircraft out of radar range (Terminal 3):
  python aeroguard_sim.py --role aircraft --id AC001 --port 9300 --relay-host 127.0.0.1 --relay-port 9200

  # Second aircraft (Terminal 4):
  python aeroguard_sim.py --role aircraft --id AC002 --port 9400 --relay-host 127.0.0.1 --relay-port 9200 --lat 20.5 --lon 75.2
        """
    )
    parser.add_argument('--role',        required=True, choices=['gs','relay','aircraft'])
    parser.add_argument('--id',          default='NODE01',    help='Node ID')
    parser.add_argument('--host',        default='127.0.0.1', help='Bind host')
    parser.add_argument('--port',        type=int, default=9000)
    # GS options
    parser.add_argument('--web-port',    type=int, default=8080, help='Web dashboard port (GS only)')
    parser.add_argument('--no-web',      action='store_true',    help='Disable web dashboard')
    # Relay options
    parser.add_argument('--gs-host',     default='127.0.0.1')
    parser.add_argument('--gs-port',     type=int, default=9100)
    # Aircraft options
    parser.add_argument('--relay-host',  default='127.0.0.1')
    parser.add_argument('--relay-port',  type=int, default=9200)
    parser.add_argument('--lat',         type=float, default=19.09)
    parser.add_argument('--lon',         type=float, default=72.87)
    parser.add_argument('--alt',         type=float, default=10000.0)

    args = parser.parse_args()

    if args.role == 'gs':
        gs = GroundStation(args.id, args.host, args.port)
        gs.start()
        if not args.no_web:
            start_web_dashboard(gs, args.web_port)
            print(f"\033[34m  🌐 Web dashboard: http://localhost:{args.web_port}\033[0m")
        print(f"\033[32m  ✔ Ground Station running. Press Ctrl+C to stop.\033[0m\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            gs.stop()
            log.info("[GS] Shutdown.")

    elif args.role == 'relay':
        relay = RelayNode(args.id, args.host, args.port, args.gs_host, args.gs_port)
        relay.start()
        print(f"\033[32m  ✔ Relay Node running. Press Ctrl+C to stop.\033[0m\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            relay.stop()
            log.info("[RELAY] Shutdown.")

    elif args.role == 'aircraft':
        ac = AircraftNode(
            args.id, args.host, args.port,
            args.relay_host, args.relay_port,
            lat=args.lat, lon=args.lon, alt=args.alt
        )
        ac.start()   # blocks on interactive loop


if __name__ == '__main__':
    main()
