"""PvZNekoInterface：插件调用 → PvZAgentService 的薄适配层。

职责：
- 把每次操作整形为稳定 dict（保证 ``summary`` / ``message`` / ``status`` 三键），
  供 ``@llm_tool`` / ``@plugin_entry`` 直接返回，避免 facade 直接碰 service 内部。
- 提供组合只读视图 ``get_readout``（状态 + 实时扫描），主模型一次调用拿到现状。

不持有业务逻辑，只做参数整形与文本归纳。观察内容由主模型直接看截图。
"""

from __future__ import annotations

from typing import Any


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _summary_from(payload: dict[str, Any]) -> str:
    return str(payload.get("summary") or payload.get("message") or payload.get("content") or "")


class PvZNekoInterface:
    def __init__(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------------ #
    #  只读
    # ------------------------------------------------------------------ #
    async def get_status(self) -> dict[str, Any]:
        payload = _as_mapping(self._service.get_status())
        summary = self._status_summary(payload)
        return {"status": "ok", "message": summary, "summary": summary, **payload}

    async def get_readout(self) -> dict[str, Any]:
        """状态 + 实时扫描的组合现状（供主模型调整打法前判断）。"""
        status = _as_mapping(self._service.get_status())
        scan = _as_mapping(self._service.scan_now())
        summary_parts = [self._status_summary(status)]
        if scan.get("summary"):
            summary_parts.append(str(scan["summary"]))
        summary = " | ".join(summary_parts)
        return {
            "status": "ok" if status.get("phase") in ("idle", "running", "paused") else "error",
            "message": summary,
            "summary": summary,
            "state": status,
            "scan": scan,
        }

    async def get_screenshot(self) -> dict[str, Any]:
        """截取最新一帧。成功时返回 ``image``(PIL) + ``width/height``；
        失败返回 error 并携带原因。"""
        try:
            img = self._service.grab_screenshot()
            return {
                "status": "ok",
                "message": "已截取最新画面。",
                "summary": "已截取最新画面。",
                "image": img,
                "width": int(img.size[0]),
                "height": int(img.size[1]),
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc), "summary": str(exc)}

    async def get_scan(self) -> dict[str, Any]:
        return _normalize(self._service.scan_now())

    # ------------------------------------------------------------------ #
    #  控制（游玩循环）
    # ------------------------------------------------------------------ #
    async def start(self, goal: str | None = None, *, restart: bool = False) -> dict[str, Any]:
        return _normalize(self._service.start(goal, restart=restart))

    async def pause(self) -> dict[str, Any]:
        return _normalize(self._service.pause(reason="neko"))

    async def resume(self) -> dict[str, Any]:
        return _normalize(self._service.resume())

    async def stop(self) -> dict[str, Any]:
        return _normalize(self._service.stop(reason="neko"))

    async def set_goal(self, goal: str) -> dict[str, Any]:
        return _normalize(self._service.set_goal(goal))

    async def give_instruction(self, instruction: str) -> dict[str, Any]:
        return _normalize(self._service.inject_instruction(instruction))

    async def set_speed(self, speed: float) -> dict[str, Any]:
        return _normalize(self._service.set_speed(speed))

    # ------------------------------------------------------------------ #
    #  辅助
    # ------------------------------------------------------------------ #
    def _status_summary(self, status: dict[str, Any]) -> str:
        phase = status.get("phase", "idle")
        goal = str(status.get("goal") or "")
        steps = int(status.get("steps") or 0)
        win = _as_mapping(status.get("window"))
        title = str(win.get("title") or "")
        window_part = f"窗口: {title}" if title else "窗口: 未找到"
        ready_part = "AI 决策就绪" if status.get("ready") else "AI 决策未就绪（需在 pvz/.env 配置）"
        goal_part = f"目标: {goal}" if goal else "目标: 未设置"
        return f"phase={phase} | {goal_part} | 步数={steps} | {window_part} | {ready_part}"


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """保证返回 dict 带 summary/message/status 三键。"""
    payload = _as_mapping(payload)
    payload.setdefault("summary", _summary_from(payload))
    payload.setdefault("message", payload["summary"])
    payload.setdefault("status", "ok")
    return payload


__all__ = ["PvZNekoInterface"]
