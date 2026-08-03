from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from ..vts_manager import VTSManager


@dataclass
class VTSEmotionTool(FunctionTool[AstrAgentContext]):
    name: str = Field(default="vts_emotion")
    description: str = Field(default="Use VTS to send emotion to the viewer.")
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "emotion_id": {
                    "type": "string",
                    "description": "The id of the emotion to send to the viewer.",
                }
            },
            "required": ["emotion_id"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        VTS_manager = VTSManager()
        await VTS_manager.send_emotion(kwargs["emotion_id"])
        return "Emotion sent."
