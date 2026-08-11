"""Agent A：客观描述游戏现状（仅给用户看，不参与执行）。"""

from __future__ import annotations

from .vlm import VLMClient

_NARRATOR_SYSTEM = """你是《植物大战僵尸》游戏画面的观察员。收到一张截图，用简洁中文描述现状，给人类用户看。

依次覆盖（没有就跳过）：
1. 界面：主菜单/关卡选择/选卡/战斗/胜利/失败/其他。
2. 阳光约多少。
3. 场上植物（哪几行、什么植物）。
4. 僵尸（哪几行、类型、威胁）。
5. 卡片栏状态。
6. 异常（弹窗、倒计时等）。

要求：60~120 字，直接描述，不要建议、不要分析策略、不要输出任何指令。
看不清就说"画面模糊/不确定"，不要编造。
"""


class Narrator:
    """一次截图 → 一段中文现状描述。

    职责边界：只描述、不指挥。返回值仅打印给用户，绝不注入 Agent B。
    """

    def __init__(self, vlm: VLMClient, mime: str = "image/png") -> None:
        self.vlm = vlm
        self._mime = mime

    def describe(self, img_b64: str) -> str:
        """返回一段中文现状描述。"""
        history = [{"role": "system", "content": _NARRATOR_SYSTEM}]
        content, _reasoning = self.vlm.chat_with_image(
            img_b64=img_b64,
            history=history,
            user_text="请描述这张截图的现状。",
            include_image=True,
            mime=self._mime,
        )
        return content.strip() or "（描述为空）"
