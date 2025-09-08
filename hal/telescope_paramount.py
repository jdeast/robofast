import socket
from typing import Tuple, Optional, Dict

"""

This is intended to be a low-level code that implements the core features of the Paramount mount in a general way so it can interface with a generic high-level observatory code that can control any telescope by swapping out this class:

import paramount
telescope = paramount.Paramount()

"""

class Paramount:
    """
    Minimal TheSkyX TCP client for Paramount mounts.

    It sends JavaScript to TheSkyX's embedded engine over a TCP socket.
    For reliability, each call opens a fresh socket and reads until EOF
    (works best if TheSkyX has 'TCP responses close socket' enabled).

    Docs:
      - Running JS over sockets (special markers): '/* Java Script */',
        optional '/* Socket Start Packet */' ... '/* Socket End Packet */'.
      - Telescope control via sky6RASCOMTele: Connect, GetRaDec, SlewToRaDec, Park, etc.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 3040, timeout: float = 10.0,
                 use_packet_markers: bool = True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.use_packet_markers = use_packet_markers

    # ---------- Low-level socket/JS helpers ----------

    def _build_script(self, body_js: str) -> bytes:
        """
        Wrap a JS body with the required preamble and optional packet markers.
        JS should assign a string to Out (e.g., Out='OK' or Out='12.3,45.6').
        """
        if self.use_packet_markers:
            script = (
                "/* Java Script */\n"
                "/* Socket Start Packet */\n"
                "var Out;\n"
                f"{body_js}\n"
                "/* Socket End Packet */\n"
            )
        else:
            script = (
                "/* Java Script */\n"
                "var Out;\n"
                f"{body_js}\n"
            )
        return script.encode("utf-8")

    def _send(self, body_js: str) -> str:
        """
        Send one JS transaction and return the raw text TheSkyX replies with.
        Designed for 'TCP responses close socket = True' so we read to EOF.
        """
        payload = self._build_script(body_js)
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)  # we're done sending
            chunks = []
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks.append(data)
        reply = b"".join(chunks).decode("utf-8", errors="replace").strip()
        return reply

    def _ok(self, body_js: str) -> bool:
        return self._send(body_js + "\nOut='OK';").endswith("OK")

    def _expect_out_csv(self, body_js: str) -> Tuple[str, ...]:
        """
        Run JS that sets Out to a comma-separated string and return tuple fields.
        """
        reply = self._send(body_js)
        # Many scripts just return the Out string as the entire reply.
        # To be resilient, take the last line.
        line = reply.splitlines()[-1] if reply else ""
        return tuple(x.strip() for x in line.split(","))

    # ---------- High-level mount API (sky6RASCOMTele) ----------

    def connect(self) -> bool:
        """Connects the mount (unparks automatically on many Paramount models)."""
        return self._ok("sky6RASCOMTele.Connect();")

    def disconnect(self) -> bool:
        """Releases this client’s handle; TheSkyX’s mount link stays up for other clients."""
        return self._ok("sky6RASCOMTele.Disconnect();")

    def is_connected(self) -> bool:
        (val,) = self._expect_out_csv("Out = sky6RASCOMTele.IsConnected;")
        return val not in ("0", "false", "False", "")

    def find_home(self) -> bool:
        return self._ok("sky6RASCOMTele.FindHome();")

    def abort(self) -> bool:
        return self._ok("sky6RASCOMTele.Abort();")

    def get_radec(self) -> Tuple[float, float]:
        """Return (RA_hours, Dec_degs)."""
        csv = self._expect_out_csv(
            "sky6RASCOMTele.GetRaDec(); Out = sky6RASCOMTele.dRa + ',' + sky6RASCOMTele.dDec;"
        )
        ra_h = float(csv[0]); dec_d = float(csv[1])
        return ra_h, dec_d

    def get_azalt(self) -> Tuple[float, float]:
        """Return (Az_degs, Alt_degs)."""
        csv = self._expect_out_csv(
            "sky6RASCOMTele.GetAzAlt(); Out = sky6RASCOMTele.dAz + ',' + sky6RASCOMTele.dAlt;"
        )
        az = float(csv[0]); alt = float(csv[1])
        return az, alt

    def slew_radec(self, ra_hours: float, dec_degs: float, name: str = "Python") -> bool:
        """Slew to JNow RA/Dec (hours, degrees)."""
        return self._ok(f"sky6RASCOMTele.SlewToRaDec({ra_hours},{dec_degs},'{name}');")

    def slew_azalt(self, az_degs: float, alt_degs: float, name: str = "Python") -> bool:
        return self._ok(f"sky6RASCOMTele.SlewToAzAlt({az_degs},{alt_degs},'{name}');")

    def sync_radec(self, ra_hours: float, dec_degs: float, name: str = "Sync") -> bool:
        return self._ok(f"sky6RASCOMTele.Sync({ra_hours},{dec_degs},'{name}');")

    def park(self) -> bool:
        """Parks and (per Bisque behavior) may drop the mount connection in TheSkyX."""
        return self._ok("sky6RASCOMTele.ParkAndDoNotDisconnect();")

    def unpark(self) -> bool:
        return self._ok("sky6RASCOMTele.Unpark();")

    def is_parked(self) -> bool:
        (val,) = self._expect_out_csv("Out = sky6RASCOMTele.IsParked();")
        return val not in ("0", "false", "False", "")

    def set_tracking(self, on: bool = True,
                     use_current_rates: bool = True,
                     ra_rate_arcsec_per_sec: float = 0.0,
                     dec_rate_arcsec_per_sec: float = 0.0) -> bool:
        """
        Set tracking. If use_current_rates=True, RA/Dec rates are ignored and TheSkyX’s
        configured rate (usually sidereal) is used.
        """
        lon = 1 if on else 0
        ignore = 1 if use_current_rates else 0
        return self._ok(
            f"sky6RASCOMTele.SetTracking({lon},{ignore},{ra_rate_arcsec_per_sec},{dec_rate_arcsec_per_sec});"
        )

    def jog(self, arcmin: float, direction: str) -> bool:
        """
        Jog by arcminutes in direction: 'N','S','E','W','L','R','U','D'.
        """
        return self._ok(f"sky6RASCOMTele.Jog({arcmin},'{direction}');")

    def status(self) -> Dict[str, object]:
        """Quick snapshot of mount state from TheSkyX."""
        ra, dec = self.get_radec()
        az, alt = self.get_azalt()
        is_conn = self.is_connected()
        (is_tracking,) = self._expect_out_csv("Out = sky6RASCOMTele.IsTracking;")
        (slew_done,)  = self._expect_out_csv("Out = sky6RASCOMTele.IsSlewComplete;")
        (in_limit,)   = self._expect_out_csv("Out = sky6RASCOMTele.IsInLimit;")
        return {
            "connected": is_conn,
            "parked": self.is_parked(),
            "tracking": is_tracking not in ("0", "false", "False", ""),
            "slew_complete": slew_done not in ("0", "false", "False", ""),
            "in_limit": in_limit not in ("0", "false", "False", ""),
            "ra_hours": ra,
            "dec_degs": dec,
            "az_degs": az,
            "alt_degs": alt,
        }

# ---------- Example ----------
if __name__ == "__main__":
    mount = Paramount()
    if mount.connect():
        print("Connected:", mount.is_connected())
        print("Status:", mount.status())
        # Example slew (RA=10.6847h, Dec=41.2687°: roughly M31):
        # mount.slew_radec(10.6847, 41.2687)
        mount.disconnect()
