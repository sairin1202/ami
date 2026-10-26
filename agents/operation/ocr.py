"""屏幕区域文字识别：mss 截取屏幕区域 → macOS Vision 本地识别，返回屏幕 point 坐标的文字框。

只截屏幕上看得到的内容，所以调用前要保证目标窗口在最前面。
Vision 的快速模式只支持少数拉丁语言，不支持中文；识别中文时始终用精确模式，靠只识别小区域提速。
"""

from dataclasses import dataclass

import io

import mss
import mss.tools
import Quartz
import Vision
from Foundation import NSData


class OCRError(RuntimeError):
    pass


@dataclass
class Box:
    text: str
    x: float
    y: float
    w: float
    h: float
    conf: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def has_permission() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def recognize(region: tuple[float, float, float, float], fast: bool = False) -> list[Box]:
    """识别屏幕区域 region=(x, y, w, h)（point）里的文字。fast=True 只适合纯英文数字。"""
    if not has_permission():
        raise OCRError("没有「屏幕录制」权限，无法识别屏幕文字。请在 系统设置 → 隐私与安全性 → 屏幕录制 里"
                       "给启动本程序的 App（终端 / iTerm / Claude）打开，然后重启那个 App")
    ox, oy, ow, oh = region
    with (getattr(mss, "MSS", None) or mss.mss)() as sct:
        raw = sct.grab({"left": int(ox), "top": int(oy), "width": int(ow), "height": int(oh)})
    if raw.size[0] < ow * 1.5:
        # 非 Retina 屏（1 像素 = 1 point）上字很小，大区域交给 Vision 时会被缩小、认错字（「腾讯新闻」→「鹏讯新间」）；
        # 先放大一倍再识别。框的坐标是按比例算的，不受影响
        from PIL import Image
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
    else:
        png = mss.tools.to_png(raw.rgb, raw.size)
    data = NSData.dataWithBytes_length_(png, len(png))

    req = Vision.VNRecognizeTextRequest.alloc().init()
    if fast:
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
        req.setRecognitionLanguages_(["en-US"])
    else:
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise OCRError(f"文字识别失败: {err}")

    boxes = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        c = cand[0]
        bb = obs.boundingBox()  # 归一化坐标，原点在左下角
        boxes.append(Box(
            text=str(c.string()).strip(),
            x=ox + bb.origin.x * ow,
            y=oy + (1 - bb.origin.y - bb.size.height) * oh,
            w=bb.size.width * ow, h=bb.size.height * oh, conf=float(c.confidence())))
    boxes.sort(key=lambda b: (round(b.y / 4), b.x))
    return boxes
