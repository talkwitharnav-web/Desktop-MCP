from dataclasses import dataclass
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
import logging
import os
from _ctypes import COMError

from PIL import Image, ImageGrab

try:
    import dxcam
except Exception:
    dxcam = None

try:
    _DXCAM_VERSION = version("dxcam")
except PackageNotFoundError:
    _DXCAM_VERSION = None

try:
    import mss
except ImportError:
    mss = None

import windows_mcp.uia as uia

logger = logging.getLogger(__name__)

_BOUNDED_DXCAM_VERSION = "0.3.0"


class _DxcamRecoveryRequired(RuntimeError):
    """A one-shot DXGI camera must yield to a fallback, not retry indefinitely."""


class _NoDisplayRecovery:
    def handle(self, **kwargs: object) -> None:
        raise _DxcamRecoveryRequired("DXGI display recovery is required; use a capture fallback.")


class _CheckpointCancelled(BaseException):
    def __init__(self, cause: BaseException) -> None:
        self.cause = cause


class _CaptureCheckpoint:
    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self.callback = callback

    def __call__(self) -> None:
        if self.callback is not None:
            try:
                self.callback()
            except BaseException as error:
                # Native failure handlers must never turn revocation into another capture.
                raise _CheckpointCancelled(error) from error


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DxcamOutput:
    device_idx: int
    output_idx: int
    rect: uia.Rect


def _build_crop_box(capture_rect: uia.Rect, padding: int = 0) -> tuple[int, int, int, int]:
    left_offset, top_offset, _, _ = uia.GetVirtualScreenRect()
    return (
        capture_rect.left - left_offset + padding,
        capture_rect.top - top_offset + padding,
        capture_rect.right - left_offset + padding,
        capture_rect.bottom - top_offset + padding,
    )


def _crop_screenshot(screenshot: Image.Image, capture_rect: uia.Rect | None) -> Image.Image:
    if capture_rect is None:
        return screenshot
    return screenshot.crop(_build_crop_box(capture_rect))


def get_screenshot_backend() -> str:
    """Read the preferred backend from the environment variable."""
    value = os.getenv("WINDOWS_MCP_SCREENSHOT_BACKEND", "auto")
    normalized = value.strip().lower()
    valid = _ScreenshotBackend.registry.keys() | {"auto"}
    if normalized in valid:
        return normalized
    logger.warning(
        "Unknown screenshot backend '%s'; falling back to auto",
        value,
    )
    return "auto"


# ---------------------------------------------------------------------------
# Backend framework
# ---------------------------------------------------------------------------


class _ScreenshotBackend:
    """Base class for screenshot capture backends.

    Subclasses **must** define two class attributes:

    * ``name: str`` – unique key such as ``"dxcam"``.
    * ``priority: int`` – lower numbers are tried first in the *auto* chain.

    Defining both attributes automatically registers the subclass via
    ``__init_subclass__``.
    """

    name: str
    priority: int

    registry: dict[str, type["_ScreenshotBackend"]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and "priority" in cls.__dict__:
            existing = _ScreenshotBackend.registry.get(cls.name)
            if existing is not None and existing is not cls:
                raise ValueError(f"Duplicate screenshot backend name: {cls.name!r}")
            _ScreenshotBackend.registry[cls.name] = cls

    def is_available(self, capture_rect: uia.Rect | None) -> bool:
        """Return ``True`` if this backend can service the request."""
        return True

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        """Capture a screenshot.  Subclasses must override."""
        raise NotImplementedError


class _DxcamBackend(_ScreenshotBackend):
    """DXGI-based capture via the *dxcam* library."""

    name = "dxcam"
    priority = 10

    def __init__(self) -> None:
        self._camera_cache: dict[tuple[int, int], object] = {}

    @staticmethod
    def _iter_outputs() -> list[DxcamOutput]:
        if dxcam is None:
            return []

        factory = getattr(dxcam, "__factory", None)
        if factory is None:
            return []

        outputs: list[DxcamOutput] = []
        for device_idx, device_outputs in enumerate(getattr(factory, "outputs", [])):
            for output_idx, output in enumerate(device_outputs):
                try:
                    output.update_desc()
                    coordinates = output.desc.DesktopCoordinates
                    if not output.attached_to_desktop:
                        continue
                except AttributeError, OSError, RuntimeError, ValueError, COMError:
                    logger.debug(
                        "Failed to read DXGI output geometry for device=%s output=%s",
                        device_idx,
                        output_idx,
                        exc_info=True,
                    )
                    continue
                outputs.append(
                    DxcamOutput(
                        device_idx=device_idx,
                        output_idx=output_idx,
                        rect=uia.Rect(
                            coordinates.left,
                            coordinates.top,
                            coordinates.right,
                            coordinates.bottom,
                        ),
                    )
                )
        return outputs

    @classmethod
    def _resolve_region(
        cls,
        capture_rect: uia.Rect,
    ) -> tuple[int, int, tuple[int, int, int, int] | None] | None:
        """Return ``(device_idx, output_idx, region)`` when one DXGI output contains the rect."""
        for output in cls._iter_outputs():
            output_rect = output.rect
            if (
                output_rect.left <= capture_rect.left
                and output_rect.top <= capture_rect.top
                and output_rect.right >= capture_rect.right
                and output_rect.bottom >= capture_rect.bottom
            ):
                if output_rect == capture_rect:
                    return output.device_idx, output.output_idx, None
                return (
                    output.device_idx,
                    output.output_idx,
                    (
                        capture_rect.left - output_rect.left,
                        capture_rect.top - output_rect.top,
                        capture_rect.right - output_rect.left,
                        capture_rect.bottom - output_rect.top,
                    ),
                )
        return None

    def is_available(self, capture_rect: uia.Rect | None) -> bool:
        if dxcam is None or _DXCAM_VERSION != _BOUNDED_DXCAM_VERSION:
            return False
        if capture_rect is None:
            return False
        return self._resolve_region(capture_rect) is not None

    def _get_camera(self, device_idx: int, output_idx: int) -> object:
        if _DXCAM_VERSION != _BOUNDED_DXCAM_VERSION:
            raise RuntimeError("This DXCAM version has not been verified for bounded capture.")
        camera_key = (device_idx, output_idx)
        camera = self._camera_cache.get(camera_key)
        if camera is None:
            camera = dxcam.create(
                device_idx=device_idx,
                output_idx=output_idx,
                processor_backend="numpy",
            )
            if camera.is_capturing:
                raise RuntimeError("A threaded DXCAM camera cannot be used for guarded capture.")
            if not callable(getattr(getattr(camera, "_display_recovery", None), "handle", None)):
                camera.release()
                raise RuntimeError("DXCAM does not expose the verified one-shot recovery boundary.")
            # DXCAM 0.3.0 otherwise retries display recovery forever inside grab().
            camera._display_recovery = _NoDisplayRecovery()
            self._camera_cache[camera_key] = camera
        if camera.is_capturing:
            raise RuntimeError("A threaded DXCAM camera cannot be used for guarded capture.")
        return camera

    def capture(
        self,
        capture_rect: uia.Rect | None,
        *,
        checkpoint: _CaptureCheckpoint | None = None,
    ) -> Image.Image:
        checkpoint = checkpoint or _CaptureCheckpoint()
        resolved = self._resolve_region(capture_rect)
        if resolved is None:
            raise ValueError(
                "DXGI capture supports only regions fully contained within one display"
            )
        device_idx, output_idx, region = resolved
        checkpoint()
        camera = self._get_camera(device_idx, output_idx)
        checkpoint()
        try:
            frame = camera.grab(region=region, copy=True, new_frame_only=False)
            if frame is None:
                raise RuntimeError("DXGI capture returned no frame")
        except OSError, RuntimeError, ValueError, COMError:
            self._camera_cache.pop((device_idx, output_idx), None)
            try:
                camera.release()
            except OSError, RuntimeError, COMError:
                logger.warning("Failed to release a rejected one-shot DXGI camera", exc_info=True)
            raise
        checkpoint()
        return Image.fromarray(frame)


class _PillowBackend(_ScreenshotBackend):
    """Capture via PIL *ImageGrab* (always available)."""

    name = "pillow"
    priority = 100

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        grab_kwargs: dict[str, object] = {"all_screens": True}
        if capture_rect is not None:
            grab_kwargs["bbox"] = (
                capture_rect.left,
                capture_rect.top,
                capture_rect.right,
                capture_rect.bottom,
            )
        try:
            screenshot = ImageGrab.grab(**grab_kwargs)
        except OSError, RuntimeError, ValueError:
            if capture_rect is not None:
                logger.warning(
                    "Failed to capture selected region directly, "
                    "falling back to virtual screen crop"
                )
                # Fallback: grab full virtual screen then crop to the requested region.
                return _crop_screenshot(ImageGrab.grab(all_screens=True), capture_rect)
            logger.warning("Failed to capture virtual screen, using primary screen")
            screenshot = ImageGrab.grab()
        # Success path: ImageGrab.grab(bbox=...) already returned the exact region,
        # so no further cropping is needed.
        return screenshot


class _MssBackend(_ScreenshotBackend):
    """Capture via the *mss* library."""

    name = "mss"
    priority = 20

    def is_available(self, capture_rect: uia.Rect | None) -> bool:
        return mss is not None

    def capture(self, capture_rect: uia.Rect | None) -> Image.Image:
        if mss is None:
            raise RuntimeError("mss is not available")
        with mss.mss() as sct:
            if capture_rect is None:
                monitor = sct.monitors[0]
            else:
                monitor = {
                    "left": capture_rect.left,
                    "top": capture_rect.top,
                    "width": capture_rect.right - capture_rect.left,
                    "height": capture_rect.bottom - capture_rect.top,
                }
            raw = sct.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        # mss.grab(monitor) already captures exactly the requested region,
        # so no further cropping is needed.
        return image


# ---------------------------------------------------------------------------
# Instance management
# ---------------------------------------------------------------------------

_backend_instances: dict[str, _ScreenshotBackend] = {}

#: Backends that have handed back an unusable frame in this process.
#: On VM/RDP desktops dxcam can initialize successfully and then return empty
#: frames from then on (issue #371). Retrying it on every call just reproduces
#: the same broken capture, so a backend caught doing this is skipped for the
#: rest of the process and the chain moves on to mss.
_degraded_backends: set[str] = set()


def _get_backend(name: str) -> _ScreenshotBackend:
    """Return a cached singleton instance for the given backend *name*."""
    if name not in _backend_instances:
        cls = _ScreenshotBackend.registry.get(name)
        if cls is None:
            raise ValueError(f"Unknown screenshot backend: {name!r}")
        _backend_instances[name] = cls()
    return _backend_instances[name]


def _is_usable_capture(image: Image.Image | None) -> bool:
    """Return True if a captured frame is structurally sound enough to encode.

    A backend that fails by raising is already handled by the chain below. The
    case this catches is the quiet one behind issue #371: on VM/RDP desktops
    dxcam can initialize successfully and then return frames carrying no pixel
    data, which travel all the way to the client as an undecodable image with no
    error anywhere in between.

    Only structure is checked, never content -- a legitimately black screen is a
    perfectly valid screenshot, and rejecting it would break locked and
    screensaver desktops.
    """
    if image is None:
        return False
    if image.width <= 0 or image.height <= 0:
        return False
    try:
        image.load()
    except OSError, ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def capture(
    capture_rect: uia.Rect | None,
    backend: str | None = None,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[Image.Image, str]:
    """Capture a screenshot and return ``(image, backend_name_used)``."""
    try:
        return _capture(capture_rect, backend, _CaptureCheckpoint(checkpoint))
    except _CheckpointCancelled as cancelled:
        raise cancelled.cause from None


def _capture(
    capture_rect: uia.Rect | None, backend: str | None, checkpoint: _CaptureCheckpoint
) -> tuple[Image.Image, str]:
    selected = backend or get_screenshot_backend()

    # Build the candidate chain: all registered backends sorted by priority, or a single one.
    if selected == "auto":
        chain = sorted(_ScreenshotBackend.registry.values(), key=lambda c: c.priority)
    else:
        cls = _ScreenshotBackend.registry.get(selected)
        if cls is None:
            raise ValueError(f"Unknown screenshot backend: {selected!r}")
        chain = [cls]

    # Try each candidate: skip unavailable ones, catch failures and fall through.
    for backend_cls in chain:
        checkpoint()
        if backend_cls.name in _degraded_backends:
            continue
        inst = _get_backend(backend_cls.name)
        if not inst.is_available(capture_rect):
            continue
        checkpoint()
        try:
            if isinstance(inst, _DxcamBackend):
                image = inst.capture(capture_rect, checkpoint=checkpoint)
            else:
                image = inst.capture(capture_rect)
        except OSError, RuntimeError, ValueError, IndexError, COMError:
            checkpoint()
            if inst.name == "dxcam":
                _degraded_backends.add(inst.name)
            logger.warning(
                "Screenshot backend '%s' failed; trying next backend",
                inst.name,
                exc_info=selected != "auto",
            )
            continue
        try:
            checkpoint()
        except _CheckpointCancelled:
            if isinstance(image, Image.Image):
                image.close()
            raise

        # A backend can also fail silently, returning a frame with nothing in it.
        # Treat that exactly like a raised failure rather than shipping bytes the
        # client cannot decode.
        if not _is_usable_capture(image):
            _degraded_backends.add(inst.name)
            logger.warning(
                "Screenshot backend '%s' returned an unusable frame; disabling it "
                "for this process and trying the next backend",
                inst.name,
            )
            continue

        return image, inst.name

    # All candidates exhausted — pillow is always present as the last resort.
    checkpoint()
    image = _get_backend("pillow").capture(capture_rect)
    try:
        checkpoint()
    except _CheckpointCancelled:
        image.close()
        raise
    return image, "pillow"
