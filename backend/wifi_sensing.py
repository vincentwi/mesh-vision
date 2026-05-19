"""
WiFi Sensing Engine — Pure Real-Data RSSI Analysis, Environment Mapping,
and Human Presence Estimation via CoreWLAN.

NO SIMULATIONS. NO FAKE DATA. All signal processing operates on real
WiFi RSSI measurements from the macOS CoreWLAN interface.

Three sensing engines:
  1. FastRssiMonitor    — 10 Hz RSSI stream from the connected AP
  2. EnvironmentMapper  — Full WiFi landscape from periodic slow scans
  3. HumanPresenceEstimator — Fuses both sources for presence/activity

Plus:
  - build_sensing_payload() — Aggregates all sensing data into one dict

Signal processing math:
  - Butterworth bandpass filter implemented via bilinear transform (no scipy)
  - FFT-based frequency estimation for breathing/heart rate
  - Exponential moving averages for motion tracking
  - Linear regression for RSSI trend detection
  - Environment fingerprinting via sorted BSSID/RSSI hashing

Python 3.9 compatible. Thread-safe. Numpy for signal processing.
"""

import hashlib
import logging
import math
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger('wifi_sensing')

# Try to import CoreWLAN (macOS only)
try:
    import CoreWLAN  # type: ignore
    _HAS_COREWLAN = True
except ImportError:
    _HAS_COREWLAN = False
    logger.info('CoreWLAN not available — FastRssiMonitor will not auto-start')


# ---------------------------------------------------------------------------
# Numpy-only Butterworth bandpass filter (no scipy dependency)
# ---------------------------------------------------------------------------

def _butter_bandpass_coefficients(
    lowcut: float, highcut: float, fs: float, order: int = 2
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Design a digital Butterworth bandpass filter using the bilinear transform.

    Math overview:
      1. Pre-warp the analog cutoff frequencies:
         Ω = 2·fs·tan(π·f/fs)
      2. Design analog Butterworth lowpass prototype poles at unit circle.
      3. Transform lowpass → bandpass in the analog domain.
      4. Apply bilinear transform s → 2·fs·(z-1)/(z+1) to get digital coefficients.
      5. Cascade second-order sections into a single transfer function.

    For order=2, this produces a 4th-order bandpass (2 pole pairs).

    Parameters:
        lowcut:  Lower cutoff frequency (Hz)
        highcut: Upper cutoff frequency (Hz)
        fs:      Sample rate (Hz)
        order:   Butterworth order (default 2, giving 4th-order bandpass)

    Returns:
        (b, a) numerator and denominator polynomial coefficients
    """
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq

    # Clamp to valid range
    low = max(low, 0.001)
    high = min(high, 0.999)

    if low >= high:
        # Degenerate: return passthrough
        return np.array([1.0]), np.array([1.0])

    # Pre-warp
    w_low = 2.0 * fs * math.tan(math.pi * low / (2.0 * fs / nyq * fs) * fs / fs)
    w_high = 2.0 * fs * math.tan(math.pi * high / (2.0 * fs / nyq * fs) * fs / fs)

    # Simpler: use tangent pre-warping directly
    w_low = math.tan(math.pi * lowcut / fs)
    w_high = math.tan(math.pi * highcut / fs)

    bw = w_high - w_low
    w0 = math.sqrt(w_low * w_high)  # center frequency (geometric mean)

    # For a 2nd-order Butterworth bandpass, we get a 4th-order digital filter.
    # Direct form coefficients via bilinear transform of analog bandpass:
    #
    # H(s) = (bw·s) / (s² + bw·s + w0²)  [single 2nd-order section]
    #
    # Using bilinear transform s = (z-1)/(z+1) (pre-warped already):
    # Substituting and collecting z powers gives us b[] and a[] coefficients.

    # Single second-order section (order=1 bandpass = 2nd order digital)
    # For each order, cascade one section
    b_total = np.array([1.0])
    a_total = np.array([1.0])

    for k in range(order):
        # Butterworth pole angle for section k
        theta = math.pi * (2.0 * k + 1) / (2.0 * order)
        # Analog prototype pole: s_k = -sin(θ) + j·cos(θ)
        sigma = math.sin(theta)

        # Bandpass transform: each analog pole becomes a conjugate pair
        # H_bp(s) = (bw·s) / (s² + sigma·bw·s + w0²)
        # Bilinear: s = (1 - z⁻¹)/(1 + z⁻¹)

        a0 = 1.0 + sigma * bw + w0 * w0
        a1 = 2.0 * (w0 * w0 - 1.0)
        a2 = 1.0 - sigma * bw + w0 * w0

        b0 = bw
        b1 = 0.0
        b2 = -bw

        # Normalize
        b_sec = np.array([b0 / a0, b1 / a0, b2 / a0])
        a_sec = np.array([1.0, a1 / a0, a2 / a0])

        # Convolve to cascade sections
        b_total = np.convolve(b_total, b_sec)
        a_total = np.convolve(a_total, a_sec)

    return b_total, a_total


def _apply_filter(b: np.ndarray, a: np.ndarray, x: np.ndarray) -> np.ndarray:
    """
    Apply IIR filter (b, a) to signal x using direct form II transposed.

    This is a forward-only filter (causal). For zero-phase filtering,
    call twice (forward + reverse), which is what we do for vital sign
    extraction to avoid phase distortion.

    y[n] = b[0]·x[n] + b[1]·x[n-1] + ... - a[1]·y[n-1] - a[2]·y[n-2] - ...
    """
    n = len(x)
    nb = len(b)
    na = len(a)
    y = np.zeros(n, dtype=np.float64)

    for i in range(n):
        acc = 0.0
        for j in range(nb):
            if i - j >= 0:
                acc += b[j] * x[i - j]
        for j in range(1, na):
            if i - j >= 0:
                acc -= a[j] * y[i - j]
        y[i] = acc

    return y


def _filtfilt(b: np.ndarray, a: np.ndarray, x: np.ndarray) -> np.ndarray:
    """
    Zero-phase digital filter: apply filter forward, then backward.
    Eliminates phase distortion — critical for extracting periodic signals
    like breathing from RSSI.
    """
    y_fwd = _apply_filter(b, a, x)
    y_rev = _apply_filter(b, a, y_fwd[::-1])
    return y_rev[::-1]


# ---------------------------------------------------------------------------
# Helper: linear regression slope
# ---------------------------------------------------------------------------

def _linear_slope(x: np.ndarray, y: np.ndarray) -> float:
    """
    Compute slope of y = a·x + b via ordinary least squares.

    slope = (N·Σ(xy) - Σx·Σy) / (N·Σ(x²) - (Σx)²)
    """
    n = len(x)
    if n < 2:
        return 0.0
    sx = np.sum(x)
    sy = np.sum(y)
    sxx = np.sum(x * x)
    sxy = np.sum(x * y)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return 0.0
    return float((n * sxy - sx * sy) / denom)


# ---------------------------------------------------------------------------
# Helper: hash-based azimuth for BSSIDs (deterministic sector assignment)
# ---------------------------------------------------------------------------

def _bssid_to_sector(bssid: str, n_sectors: int = 12) -> int:
    """
    Map a BSSID to a direction sector (0..n_sectors-1) via hash.

    Since we don't have actual angle-of-arrival data from a single WiFi
    antenna, we use a deterministic hash to assign each AP a pseudo-azimuth.
    This gives consistent spatial distribution for environment fingerprinting,
    even though the actual physical direction is unknown.
    """
    h = int(hashlib.md5(bssid.encode()).hexdigest()[:8], 16)
    return h % n_sectors


def _bssid_to_azimuth(bssid: str) -> float:
    """Map BSSID to a pseudo-azimuth in degrees [0, 360)."""
    sector = _bssid_to_sector(bssid, 360)
    return float(sector)


# ---------------------------------------------------------------------------
# 1. FastRssiMonitor — 10 Hz RSSI stream from connected AP
# ---------------------------------------------------------------------------

class FastRssiMonitor:
    """
    Reads RSSI from the connected AP at ~10 Hz (100 ms intervals) using
    CoreWLAN's iface.rssiValue(). This requires NO scanning — it reads
    the current association's signal strength, which fluctuates with
    human movement in the environment.

    Computes in real-time:
      - Breathing detection (bandpass 0.15–0.5 Hz = 9–30 BPM)
      - Heart rate estimation (bandpass 0.8–2.0 Hz, experimental)
      - Motion detection (1-second running variance)
      - Presence detection (30-second variance vs baseline noise)
      - SNR (RSSI − noise floor)
      - Sparkline data (last 120 readings = 12 seconds)

    Signal processing notes:
      The RSSI from a connected AP fluctuates by ±1–3 dBm due to multipath
      interference. Human bodies absorb/reflect 2.4 GHz radiation, causing
      subtle RSSI modulation correlated with:
        - Chest wall movement (breathing): ~0.2–0.5 Hz, amplitude ~0.3–1.0 dBm
        - Gross body motion (walking): >1 dBm variance over 1s windows
        - Heart beat: ~0.8–2.0 Hz, amplitude ~0.05–0.2 dBm (very noisy)
    """

    BUFFER_SIZE = 6000      # 10 Hz × 600 s = 10 minutes
    SAMPLE_RATE = 10.0      # Hz (target)
    SAMPLE_INTERVAL = 0.1   # seconds

    # Breathing: 9–30 BPM = 0.15–0.5 Hz
    BREATHING_LO = 0.15
    BREATHING_HI = 0.5

    # Heart rate: 48–120 BPM = 0.8–2.0 Hz (experimental, very noisy from WiFi)
    HEART_LO = 0.8
    HEART_HI = 2.0

    # Motion detection thresholds
    MOTION_WINDOW = 10          # samples (1 second at 10 Hz)
    MOTION_THRESHOLD = 0.5      # variance above this = motion detected
    MOTION_EMA_ALPHA = 0.1      # exponential moving average smoothing

    # Presence detection
    PRESENCE_WINDOW = 300       # samples (30 seconds at 10 Hz)
    BASELINE_WINDOW = 100       # samples (10 seconds for noise baseline)
    PRESENCE_THRESHOLD = 1.2    # variance ratio above noise baseline (lowered from 1.5 for weaker home signals)

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Circular buffers
        self._rssi_buf = np.full(self.BUFFER_SIZE, np.nan, dtype=np.float64)
        self._time_buf = np.full(self.BUFFER_SIZE, np.nan, dtype=np.float64)
        self._write_idx = 0
        self._count = 0

        # Noise floor tracking
        self._noise_floor: float = -92.0
        self._current_rssi: float = -100.0

        # Computed state
        self._breathing_rate: float = 0.0
        self._breathing_confidence: float = 0.0
        self._breathing_amplitude: float = 0.0
        self._heart_rate: float = 0.0
        self._heart_confidence: float = 0.0
        self._heart_amplitude: float = 0.0
        self._motion_level: float = 0.0
        self._motion_variance: float = 0.0
        self._is_moving: bool = False
        self._presence_detected: bool = False
        self._presence_confidence: float = 0.0
        self._presence_start: Optional[float] = None
        self._baseline_variance: float = 1.0  # noise baseline
        self._baseline_locked: bool = False   # True once baseline is set

        # Filter coefficients (computed once)
        self._breath_b: Optional[np.ndarray] = None
        self._breath_a: Optional[np.ndarray] = None
        self._heart_b: Optional[np.ndarray] = None
        self._heart_a: Optional[np.ndarray] = None
        self._filters_ready = False

        # Background thread
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._interface_name: str = 'en0'

    def start(self, interface_name: str = 'en0') -> bool:
        """
        Start the 10 Hz RSSI monitoring background thread.
        Returns True if started, False if CoreWLAN not available.
        """
        if not _HAS_COREWLAN:
            logger.warning('CoreWLAN not available — FastRssiMonitor cannot start')
            return False

        self._interface_name = interface_name
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name='FastRssiMonitor',
            daemon=True,
        )
        self._thread.start()
        logger.info('FastRssiMonitor started at 10 Hz on %s', interface_name)
        return True

    def stop(self) -> None:
        """Stop the background monitoring thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info('FastRssiMonitor stopped')

    def push_reading(self, rssi: float, noise_floor: float, timestamp: Optional[float] = None) -> None:
        """
        Push a single RSSI reading (for manual/external feeding).
        Use this when not using the auto-start CoreWLAN loop.
        """
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            self._rssi_buf[self._write_idx] = rssi
            self._time_buf[self._write_idx] = ts
            self._write_idx = (self._write_idx + 1) % self.BUFFER_SIZE
            self._count = min(self._count + 1, self.BUFFER_SIZE)
            self._current_rssi = rssi
            self._noise_floor = noise_floor
            self._recompute()

    def get_state(self) -> Dict[str, Any]:
        """Return the latest computed state (thread-safe snapshot)."""
        with self._lock:
            duration_s = 0.0
            if self._presence_detected and self._presence_start is not None:
                duration_s = time.time() - self._presence_start

            sparkline = self._get_recent_rssi(120)  # last 12 seconds

            return {
                'current_rssi': round(self._current_rssi, 1),
                'noise_floor': round(self._noise_floor, 1),
                'snr': round(self._current_rssi - self._noise_floor, 1),
                'sample_count': self._count,
                'breathing': {
                    'rate_bpm': round(self._breathing_rate, 1),
                    'confidence': round(self._breathing_confidence, 3),
                    'amplitude': round(self._breathing_amplitude, 3),
                },
                'heart_rate': {
                    'rate_bpm': round(self._heart_rate, 1),
                    'confidence': round(self._heart_confidence, 3),
                    'amplitude': round(self._heart_amplitude, 3),
                },
                'motion': {
                    'level': round(self._motion_level, 3),
                    'variance_1s': round(self._motion_variance, 3),
                    'is_moving': self._is_moving,
                },
                'presence': {
                    'detected': self._presence_detected,
                    'confidence': round(self._presence_confidence, 3),
                    'duration_s': round(duration_s, 1),
                },
                'rssi_sparkline': sparkline,
            }

    # -- Background thread ----------------------------------------------------

    def _monitor_loop(self) -> None:
        """Tight 10 Hz loop reading RSSI from CoreWLAN."""
        try:
            # Use the same approach that works in server.py's CoreWLAN scanner
            import objc
            from Foundation import NSBundle
            bundle = NSBundle.bundleWithPath_('/System/Library/Frameworks/CoreWLAN.framework')
            bundle.load()
            CWWiFiClient = objc.lookUpClass('CWWiFiClient')
            client = CWWiFiClient.sharedWiFiClient()
            iface = client.interface()
            if iface is None:
                logger.error('CoreWLAN interface not found via CWWiFiClient')
                self._running = False
                return
        except Exception as e:
            logger.error('Failed to open CoreWLAN interface: %s', e)
            self._running = False
            return

        logger.info('CoreWLAN interface %s opened, starting 10 Hz RSSI reads', self._interface_name)
        next_time = time.monotonic()

        while self._running:
            try:
                rssi = float(iface.rssiValue())
                noise = float(iface.noiseMeasurement())
                ts = time.time()

                self.push_reading(rssi, noise, ts)

            except Exception as e:
                logger.debug('RSSI read error: %s', e)

            # Sleep until next 100ms tick (compensate for processing time)
            next_time += self.SAMPLE_INTERVAL
            sleep_dur = next_time - time.monotonic()
            if sleep_dur > 0:
                time.sleep(sleep_dur)
            else:
                # We're behind schedule — reset timing
                next_time = time.monotonic()

    # -- Signal processing (caller holds _lock) -------------------------------

    def _get_recent(self, n: int) -> np.ndarray:
        """Get the last n RSSI values from the circular buffer."""
        available = min(n, self._count)
        if available == 0:
            return np.array([], dtype=np.float64)
        end = self._write_idx
        start = (end - available) % self.BUFFER_SIZE
        if start < end:
            return self._rssi_buf[start:end].copy()
        else:
            return np.concatenate([
                self._rssi_buf[start:],
                self._rssi_buf[:end],
            ])

    def _get_recent_rssi(self, n: int) -> List[float]:
        """Get last n RSSI values as a Python list (for JSON serialization)."""
        arr = self._get_recent(n)
        return [round(float(v), 1) for v in arr if not np.isnan(v)]

    def _ensure_filters(self) -> None:
        """Build Butterworth filter coefficients (once)."""
        if self._filters_ready:
            return
        try:
            self._breath_b, self._breath_a = _butter_bandpass_coefficients(
                self.BREATHING_LO, self.BREATHING_HI, self.SAMPLE_RATE, order=2
            )
            self._heart_b, self._heart_a = _butter_bandpass_coefficients(
                self.HEART_LO, self.HEART_HI, self.SAMPLE_RATE, order=2
            )
            self._filters_ready = True
        except Exception as e:
            logger.debug('Filter design failed: %s', e)

    def _recompute(self) -> None:
        """Recompute all derived signals. Caller holds _lock."""
        self._compute_motion()
        self._compute_presence()

        # Need at least 5 seconds of data for vital signs
        if self._count >= 50:
            self._ensure_filters()
            self._compute_breathing()
            self._compute_heart_rate()

    def _compute_motion(self) -> None:
        """
        Motion detection via 1-second running variance.

        The variance of RSSI over a 1-second window (10 samples) reflects
        the rate of change in the multipath environment. A person walking
        through the WiFi Fresnel zone causes variance > 0.5 dBm².
        Stationary environments show variance < 0.2 dBm².
        """
        recent = self._get_recent(self.MOTION_WINDOW)
        if len(recent) < 3:
            return

        var = float(np.var(recent))
        self._motion_variance = var
        self._is_moving = var > self.MOTION_THRESHOLD

        # Exponential moving average of variance → smooth motion level [0, 1]
        normalized = float(np.clip(var / 5.0, 0.0, 1.0))
        self._motion_level = (
            self.MOTION_EMA_ALPHA * normalized +
            (1.0 - self.MOTION_EMA_ALPHA) * self._motion_level
        )

    def reset_baseline(self) -> None:
        """Force-reset the presence baseline from the next 10 seconds of readings.
        Call this when changing rooms or when presence is stuck."""
        with self._lock:
            self._baseline_locked = False
            self._baseline_variance = 1.0
            self._presence_detected = False
            self._presence_confidence = 0.0
            self._presence_start = None
            # Reset sample count so baseline window re-triggers
            # We keep the RSSI buffer intact for breathing/heart rate continuity
            # but unlock the baseline so _compute_presence recalibrates
            logger.info('Presence baseline reset requested — recalibrating from next 100 samples')

    def _compute_presence(self) -> None:
        """
        Presence detection: compare 30-second RSSI variance against baseline.

        The baseline is established from the first 10 seconds of readings.
        If the current 30-second variance exceeds baseline by PRESENCE_THRESHOLD,
        a human is likely modulating the signal.

        The confidence is the ratio of current variance to baseline variance,
        clamped to [0, 1].
        """
        # Update baseline from first readings (only once, or after reset)
        if not self._baseline_locked and self._count >= 20:
            # Use a sliding window for baseline: last 100 samples
            n = min(self.BASELINE_WINDOW, self._count)
            baseline_data = self._get_recent(n)
            self._baseline_variance = max(float(np.var(baseline_data)), 0.01)
            if self._count >= self.BASELINE_WINDOW:
                self._baseline_locked = True
                logger.info('Presence baseline locked: variance=%.4f', self._baseline_variance)

        recent = self._get_recent(min(self.PRESENCE_WINDOW, self._count))
        if len(recent) < 20:
            return

        current_var = float(np.var(recent))
        ratio = current_var / max(self._baseline_variance, 0.01)

        was_present = self._presence_detected
        self._presence_detected = ratio > self.PRESENCE_THRESHOLD
        self._presence_confidence = float(np.clip(ratio / 5.0, 0.0, 1.0))

        if self._presence_detected and not was_present:
            self._presence_start = time.time()
        elif not self._presence_detected:
            self._presence_start = None

    def _compute_breathing(self) -> None:
        """
        Breathing detection via bandpass filtering at 0.15–0.5 Hz.

        Human respiration at 12–30 BPM causes periodic chest wall expansion,
        which modulates WiFi multipath by ~0.3–1.0 dBm. We bandpass filter
        the RSSI stream and find the dominant frequency via FFT.

        Algorithm:
          1. Take last 20 seconds of RSSI (200 samples)
          2. Remove DC (subtract mean)
          3. Apply zero-phase Butterworth bandpass [0.15, 0.5] Hz
          4. FFT of filtered signal
          5. Peak frequency → breathing rate (BPM)
          6. Peak power / total power → confidence
          7. RMS of filtered signal → amplitude
        """
        if self._breath_b is None or self._breath_a is None:
            return

        data = self._get_recent(200)  # 20 seconds
        if len(data) < 50:
            return

        # Remove DC
        detrended = data - np.mean(data)

        try:
            # Zero-phase bandpass filter
            filtered = _filtfilt(self._breath_b, self._breath_a, detrended)

            # FFT for frequency estimation
            n = len(filtered)
            n_fft = max(256, 1 << (n - 1).bit_length())
            spectrum = np.abs(np.fft.rfft(filtered, n=n_fft))
            freqs = np.fft.rfftfreq(n_fft, d=1.0 / self.SAMPLE_RATE)

            # Mask to breathing band
            mask = (freqs >= self.BREATHING_LO) & (freqs <= self.BREATHING_HI)
            if not np.any(mask):
                return

            band_spectrum = spectrum[mask]
            band_freqs = freqs[mask]

            peak_idx = int(np.argmax(band_spectrum))
            peak_freq = float(band_freqs[peak_idx])
            peak_power = float(band_spectrum[peak_idx])
            total_power = float(np.sum(spectrum[1:])) + 1e-12

            self._breathing_rate = peak_freq * 60.0
            self._breathing_confidence = float(np.clip(peak_power / total_power * 4.0, 0.0, 1.0))
            self._breathing_amplitude = float(np.sqrt(np.mean(filtered ** 2)))  # RMS

        except Exception:
            logger.debug('Breathing computation failed', exc_info=True)

    def _compute_heart_rate(self) -> None:
        """
        Heart rate estimation via bandpass filtering at 0.8–2.0 Hz.

        This is EXPERIMENTAL with low confidence. The heart beat causes
        ~0.05–0.2 dBm RSSI modulation, which is near the noise floor of
        WiFi RSSI quantization (typically 1 dBm steps). CSI (channel state
        information) is needed for reliable heart rate, but we include this
        as a best-effort estimate.
        """
        if self._heart_b is None or self._heart_a is None:
            return

        data = self._get_recent(200)
        if len(data) < 100:
            return

        detrended = data - np.mean(data)

        try:
            filtered = _filtfilt(self._heart_b, self._heart_a, detrended)

            n = len(filtered)
            n_fft = max(256, 1 << (n - 1).bit_length())
            spectrum = np.abs(np.fft.rfft(filtered, n=n_fft))
            freqs = np.fft.rfftfreq(n_fft, d=1.0 / self.SAMPLE_RATE)

            mask = (freqs >= self.HEART_LO) & (freqs <= self.HEART_HI)
            if not np.any(mask):
                return

            band_spectrum = spectrum[mask]
            band_freqs = freqs[mask]

            peak_idx = int(np.argmax(band_spectrum))
            peak_freq = float(band_freqs[peak_idx])
            peak_power = float(band_spectrum[peak_idx])
            total_power = float(np.sum(spectrum[1:])) + 1e-12

            self._heart_rate = peak_freq * 60.0
            # Low confidence cap — WiFi RSSI is too coarse for reliable HR
            self._heart_confidence = float(np.clip(peak_power / total_power * 2.0, 0.0, 0.3))
            self._heart_amplitude = float(np.sqrt(np.mean(filtered ** 2)))

        except Exception:
            logger.debug('Heart rate computation failed', exc_info=True)


# ---------------------------------------------------------------------------
# 2. EnvironmentMapper — Full WiFi landscape from slow scans
# ---------------------------------------------------------------------------

class EnvironmentMapper:
    """
    Processes full WiFi scan results (from scanForNetworksWithName_error_)
    to build a comprehensive picture of the radio environment.

    Accepts scan results every ~15 seconds (scans take ~11s + cooldown).
    For each AP, tracks RSSI history, stability, trend, and anomalies.

    Also computes:
      - AP density per direction sector (12 sectors, 30° each)
      - Signal diversity (unique AP count)
      - Environment fingerprint (changes when you move rooms)
      - Channel utilization map
    """

    MAX_SCAN_HISTORY = 30   # Last 30 scans per AP ≈ 7 minutes
    N_SECTORS = 12          # 30° each
    ANOMALY_THRESHOLD = 10  # dBm jump = anomaly

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # bssid → AP tracking data
        self._aps: Dict[str, Dict[str, Any]] = {}

        # Scan metadata
        self._scan_count = 0
        self._last_scan_time: float = 0.0
        self._noise_floor: float = -92.0

        # Environment fingerprint
        self._fingerprint: str = ''
        self._baseline_fingerprint: str = ''
        self._room_changed: bool = False
        self._fingerprint_set: bool = False

        # Anomalies from last scan
        self._anomalies: List[Dict[str, Any]] = []

    def ingest_scan(
        self,
        aps: List[Dict[str, Any]],
        noise_floor: float = -92.0,
    ) -> None:
        """
        Ingest a full WiFi scan result.

        Each AP dict should contain:
          - ssid: str
          - bssid: str (MAC address)
          - rssi: int/float (dBm)
          - channel: int
          - channel_width: int (MHz, optional)
        """
        now = time.time()
        with self._lock:
            self._noise_floor = noise_floor
            self._last_scan_time = now
            self._scan_count += 1
            self._anomalies = []

            seen_keys = set()
            for ap_data in aps:
                bssid = ap_data.get('bssid', '').upper()
                ssid = ap_data.get('ssid', '')
                channel = int(ap_data.get('channel', 0))
                # Use BSSID if available, otherwise SSID+channel as key
                # (macOS privacy hides BSSIDs from CoreWLAN)
                if bssid:
                    ap_key = bssid
                elif ssid and channel:
                    ap_key = '{}@ch{}'.format(ssid, channel)
                else:
                    continue
                seen_keys.add(ap_key)

                rssi = float(ap_data.get('rssi', -100))
                ssid = ap_data.get('ssid', '')
                channel = int(ap_data.get('channel', 0))
                channel_width = int(ap_data.get('channel_width', 20))

                if ap_key not in self._aps:
                    self._aps[ap_key] = {
                        'ssid': ssid,
                        'bssid': bssid or ap_key,
                        'channel': channel,
                        'channel_width': channel_width,
                        'rssi_history': deque(maxlen=self.MAX_SCAN_HISTORY),
                        'time_history': deque(maxlen=self.MAX_SCAN_HISTORY),
                        'last_rssi': rssi,
                        'sector': _bssid_to_sector(ap_key, self.N_SECTORS),
                    }
                else:
                    # Update metadata (may change if AP is reconfigured)
                    self._aps[ap_key]['ssid'] = ssid or self._aps[ap_key]['ssid']
                    self._aps[ap_key]['channel'] = channel
                    self._aps[ap_key]['channel_width'] = channel_width

                ap_rec = self._aps[ap_key]
                prev_rssi = ap_rec['last_rssi']

                # Anomaly detection: sudden RSSI jump > threshold
                rssi_jump = abs(rssi - prev_rssi)
                if rssi_jump > self.ANOMALY_THRESHOLD and len(ap_rec['rssi_history']) > 2:
                    self._anomalies.append({
                        'bssid': bssid,
                        'ssid': ssid,
                        'rssi_jump': round(rssi - prev_rssi, 1),
                        'direction_sector': ap_rec['sector'],
                    })

                ap_rec['rssi_history'].append(rssi)
                ap_rec['time_history'].append(now)
                ap_rec['last_rssi'] = rssi

            # Update fingerprint
            self._update_fingerprint(seen_keys)

    def get_state(self) -> Dict[str, Any]:
        """Return the full environment state (thread-safe)."""
        with self._lock:
            return self._build_state()

    def _build_state(self) -> Dict[str, Any]:
        """Build the environment state dict. Caller holds _lock."""
        # Channel map: channel → count of APs
        channel_map: Dict[int, int] = {}
        # Sector density
        density_sectors = [0] * self.N_SECTORS
        # Strongest APs
        strongest_aps: List[Dict[str, Any]] = []

        for bssid, ap in self._aps.items():
            if len(ap['rssi_history']) == 0:
                continue

            ch = ap['channel']
            channel_map[ch] = channel_map.get(ch, 0) + 1
            density_sectors[ap['sector']] += 1

            rssi_arr = np.array(list(ap['rssi_history']), dtype=np.float64)
            time_arr = np.array(list(ap['time_history']), dtype=np.float64)

            mean_rssi = float(np.mean(rssi_arr))
            variance = float(np.var(rssi_arr))
            # Stability: inverse of variance, normalized [0, 1]
            stability = float(np.clip(1.0 / (1.0 + variance), 0.0, 1.0))

            # Trend: linear regression slope (dBm per scan)
            trend = 0.0
            if len(time_arr) >= 3:
                trend = _linear_slope(time_arr - time_arr[0], rssi_arr)

            snr = mean_rssi - self._noise_floor

            strongest_aps.append({
                'ssid': ap['ssid'],
                'bssid': bssid,
                'rssi': round(mean_rssi, 1),
                'channel': ap['channel'],
                'snr': round(snr, 1),
                'stability': round(stability, 3),
                'trend': round(trend, 4),
                'variance': round(variance, 2),
            })

        # Sort by RSSI descending, take top 10
        strongest_aps.sort(key=lambda x: x['rssi'], reverse=True)
        top_aps = strongest_aps[:10]

        return {
            'ap_count': len(self._aps),
            'scan_count': self._scan_count,
            'channel_map': channel_map,
            'density_sectors': density_sectors,
            'signal_diversity': len(self._aps),
            'room_fingerprint_hash': self._fingerprint[:6] if self._fingerprint else '',
            'room_changed': self._room_changed,
            'strongest_aps': top_aps,
            'anomalies': list(self._anomalies),
            'noise_floor': round(self._noise_floor, 1),
        }

    def _update_fingerprint(self, seen_bssids: set) -> None:
        """
        Update the environment fingerprint.

        The fingerprint is a hash of sorted (bssid, rounded_mean_rssi) pairs.
        When you move rooms, the set of visible APs and their signal strengths
        change significantly. We detect this by comparing against a baseline
        fingerprint established after a few scans.

        Room change detection:
          Compute Jaccard similarity between current and baseline AP sets.
          If similarity drops below 80%, flag as room change.
        """
        # Build current fingerprint
        entries = []
        for bssid in sorted(seen_bssids):
            if bssid in self._aps and len(self._aps[bssid]['rssi_history']) > 0:
                avg = float(np.mean(list(self._aps[bssid]['rssi_history'])))
                # Round to 5 dBm buckets for stability
                bucket = int(round(avg / 5.0) * 5)
                entries.append(f'{bssid}:{bucket}')

        fp_str = '|'.join(entries)
        self._fingerprint = hashlib.sha256(fp_str.encode()).hexdigest()[:12]

        # Set baseline after 3rd scan
        if self._scan_count == 3:
            self._baseline_fingerprint = self._fingerprint
            self._fingerprint_set = True
            logger.info('Environment baseline fingerprint set: %s', self._fingerprint[:6])

        # Room change detection: compare CONSECUTIVE scans
        # Previous approach compared current scan (~25 APs) vs 90s window (~90 APs)
        # which gave Jaccard ~0.25-0.30 even when stationary (subset problem).
        # Fix: compare current scan's AP set against the PREVIOUS scan's AP set.
        # Two consecutive scans from the same room see very similar sets (~0.7-0.9).
        # Moving rooms drops similarity to <0.3 because different APs are visible.
        if not hasattr(self, '_prev_scan_bssids'):
            self._prev_scan_bssids = set()
            self._room_change_count = 0  # consecutive low-similarity scans

        if self._fingerprint_set and self._scan_count > 5 and len(self._prev_scan_bssids) > 0:
            if len(seen_bssids) > 0:
                intersection = len(self._prev_scan_bssids & seen_bssids)
                union = len(self._prev_scan_bssids | seen_bssids)
                similarity = intersection / max(union, 1)

                # Require 3 consecutive low-similarity scans to confirm room change
                # (single scan variance is normal — WiFi scans are noisy)
                if similarity < 0.40:
                    self._room_change_count += 1
                else:
                    self._room_change_count = 0

                was_changed = self._room_changed
                self._room_changed = self._room_change_count >= 3

                if self._room_changed and not was_changed:
                    logger.info('Room change confirmed (similarity=%.2f, %d consecutive low scans)',
                                similarity, self._room_change_count)
                    self._baseline_fingerprint = self._fingerprint
                    self._room_change_count = 0  # reset counter after confirming
                elif was_changed and not self._room_changed:
                    logger.info('Room stabilized (similarity=%.2f)', similarity)

        self._prev_scan_bssids = set(seen_bssids)


# ---------------------------------------------------------------------------
# 3. HumanPresenceEstimator — Fuse both data sources
# ---------------------------------------------------------------------------

class HumanPresenceEstimator:
    """
    Fuses FastRssiMonitor (10 Hz breathing/motion) and EnvironmentMapper
    (15s landscape scans) to estimate human presence, count, activity,
    and spatial distribution.

    Presence map: 12 sectors (30° each) with presence score [0, 1].

    Activity classification:
      - 'still':      low motion, possible breathing signal
      - 'walking':    high motion, high RSSI variance
      - 'gesturing':  medium motion with high-frequency components

    Presence blobs:
      Instead of COCO keypoints (which require CSI phase data or cameras),
      we output 'presence blobs' — areas of the radio environment where
      human activity is detected, characterized by:
        - azimuth:   pseudo-direction (from BSSID hash, 0–360°)
        - radius:    distance estimate from RSSI (0=close, 1=far)
        - intensity: presence confidence (0–1)
        - activity:  'still', 'walking', or 'gesturing'
    """

    N_SECTORS = 12

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._presence_map = [0.0] * self.N_SECTORS
        self._person_count = 0
        self._total_confidence = 0.0
        self._activity = 'unknown'
        self._blobs: List[Dict[str, Any]] = []
        self._duration_s = 0.0
        self._presence_start: Optional[float] = None

    def update(
        self,
        fast_state: Dict[str, Any],
        env_state: Dict[str, Any],
    ) -> None:
        """
        Update presence estimates from both sensing sources.

        fast_state: from FastRssiMonitor.get_state()
        env_state:  from EnvironmentMapper.get_state()
        """
        with self._lock:
            self._fuse(fast_state, env_state)

    def get_state(self) -> Dict[str, Any]:
        """Return presence estimation state (thread-safe)."""
        with self._lock:
            return {
                'person_count': self._person_count,
                'total_confidence': round(self._total_confidence, 3),
                'activity': self._activity,
                'presence_map': [round(v, 3) for v in self._presence_map],
                'blobs': list(self._blobs),
                'duration_s': round(self._duration_s, 1),
            }

    def _fuse(
        self,
        fast: Dict[str, Any],
        env: Dict[str, Any],
    ) -> None:
        """Fuse fast RSSI and environment data. Caller holds _lock."""
        # Extract fast RSSI signals
        motion_level = fast.get('motion', {}).get('level', 0.0)
        breathing_conf = fast.get('breathing', {}).get('confidence', 0.0)
        presence_conf = fast.get('presence', {}).get('confidence', 0.0)
        is_moving = fast.get('motion', {}).get('is_moving', False)
        motion_var = fast.get('motion', {}).get('variance_1s', 0.0)

        # Extract environment signals
        anomalies = env.get('anomalies', [])
        strongest_aps = env.get('strongest_aps', [])
        density_sectors = env.get('density_sectors', [0] * self.N_SECTORS)

        # --- Activity classification ---
        if motion_var > 3.0:
            self._activity = 'walking'
        elif motion_var > 1.0:
            self._activity = 'gesturing'
        elif breathing_conf > 0.2 or presence_conf > 0.3:
            self._activity = 'still'
        else:
            self._activity = 'unknown'

        # --- Person count estimation ---
        # Count APs with high variance (correlated RSSI anomalies)
        high_var_count = 0
        for ap in strongest_aps:
            if ap.get('variance', 0) > 2.0:
                high_var_count += 1

        anomaly_count = len(anomalies)

        # Heuristic: 1 person per 3-5 high-variance APs + anomalies
        person_est_from_env = max(0, (high_var_count + anomaly_count) // 4)

        # Fast RSSI says presence? At least 1 person
        person_est_from_fast = 1 if (presence_conf > 0.3 or motion_level > 0.2) else 0

        self._person_count = max(person_est_from_fast, person_est_from_env)
        if self._person_count == 0 and breathing_conf > 0.15:
            self._person_count = 1

        # --- Total confidence ---
        # Multi-signal fusion: motion × breathing × environment anomalies
        signals = [
            presence_conf,
            min(motion_level * 2.0, 1.0),
            min(breathing_conf * 1.5, 1.0),
            min(anomaly_count * 0.2, 1.0),
        ]
        self._total_confidence = float(np.clip(max(signals) * 0.7 + np.mean(signals) * 0.3, 0.0, 1.0))

        # --- Presence duration tracking ---
        if self._person_count > 0:
            if self._presence_start is None:
                self._presence_start = time.time()
            self._duration_s = time.time() - self._presence_start
        else:
            self._presence_start = None
            self._duration_s = 0.0

        # --- Presence map (12 sectors) ---
        self._presence_map = [0.0] * self.N_SECTORS

        # Distribute presence scores to sectors based on high-variance APs
        for ap in strongest_aps:
            bssid = ap.get('bssid', '')
            var = ap.get('variance', 0.0)
            if var > 0.5 and bssid:
                sector = _bssid_to_sector(bssid, self.N_SECTORS)
                # Score from variance and proximity (stronger RSSI = closer)
                rssi = ap.get('rssi', -100)
                proximity = float(np.clip((rssi + 90) / 40.0, 0.0, 1.0))
                var_score = float(np.clip(var / 5.0, 0.0, 1.0))
                self._presence_map[sector] = max(
                    self._presence_map[sector],
                    proximity * var_score,
                )

        # Anomalies contribute to presence map
        for anomaly in anomalies:
            sector = anomaly.get('direction_sector', 0)
            if 0 <= sector < self.N_SECTORS:
                jump = abs(anomaly.get('rssi_jump', 0))
                self._presence_map[sector] = max(
                    self._presence_map[sector],
                    float(np.clip(jump / 15.0, 0.0, 1.0)),
                )

        # --- Presence blobs ---
        self._blobs = []
        for sector_idx, score in enumerate(self._presence_map):
            if score > 0.2:
                azimuth = sector_idx * (360.0 / self.N_SECTORS) + 15.0  # center of sector

                # Estimate radius from strongest AP in this sector
                sector_rssis = []
                for ap in strongest_aps:
                    bssid = ap.get('bssid', '')
                    if bssid and _bssid_to_sector(bssid, self.N_SECTORS) == sector_idx:
                        sector_rssis.append(ap.get('rssi', -100))

                if sector_rssis:
                    best_rssi = max(sector_rssis)
                    # RSSI-to-distance rough mapping: -30 → 0.0 (very close), -90 → 1.0 (far)
                    radius = float(np.clip((-best_rssi - 30) / 60.0, 0.0, 1.0))
                else:
                    radius = 0.5

                self._blobs.append({
                    'azimuth': round(azimuth, 1),
                    'radius': round(radius, 3),
                    'intensity': round(score, 3),
                    'activity': self._activity,
                })


# ---------------------------------------------------------------------------
# Integration: build_sensing_payload()
# ---------------------------------------------------------------------------

def build_sensing_payload(
    fast_rssi: FastRssiMonitor,
    env_mapper: EnvironmentMapper,
    presence_est: HumanPresenceEstimator,
) -> Dict[str, Any]:
    """
    Aggregate all sensing data into a single payload dict.

    Call this at your desired output rate (e.g., 2–10 Hz).
    Each sub-engine's get_state() is thread-safe.

    Returns a dict with three top-level keys:
      - fast_rssi:   Real-time RSSI analysis (breathing, motion, presence)
      - environment:  WiFi landscape (APs, channels, fingerprint, anomalies)
      - presence:     Fused human presence estimation (count, map, blobs)
    """
    fast_state = fast_rssi.get_state()
    env_state = env_mapper.get_state()

    # Update the presence estimator with latest data
    presence_est.update(fast_state, env_state)
    pres_state = presence_est.get_state()

    return {
        'fast_rssi': fast_state,
        'environment': env_state,
        'presence': pres_state,
    }


# ---------------------------------------------------------------------------
# Convenience: start all engines
# ---------------------------------------------------------------------------

def create_sensing_stack(
    interface_name: str = 'en0',
    auto_start: bool = True,
) -> Tuple[FastRssiMonitor, EnvironmentMapper, HumanPresenceEstimator]:
    """
    Create and optionally start the full sensing stack.

    Returns (fast_rssi, env_mapper, presence_est) tuple.
    The FastRssiMonitor auto-starts its background thread if CoreWLAN is
    available and auto_start=True.

    Usage:
        fast, env, pres = create_sensing_stack()
        # ... feed env.ingest_scan() with scan results ...
        payload = build_sensing_payload(fast, env, pres)
    """
    fast = FastRssiMonitor()
    env = EnvironmentMapper()
    pres = HumanPresenceEstimator()

    if auto_start:
        started = fast.start(interface_name)
        if started:
            logger.info('Sensing stack started with 10 Hz RSSI monitor on %s', interface_name)
        else:
            logger.info('Sensing stack ready (manual RSSI feeding mode)')

    return fast, env, pres
