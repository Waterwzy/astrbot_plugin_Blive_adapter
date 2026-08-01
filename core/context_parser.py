from dataclasses import dataclass


@dataclass
class ContextParser:
    """SmartFilter上下文解析类
    2.4.0加入，用于上下文的截断和规范化
    """

    context: list[dict]
    """OpenAI格式上下文消息"""

    def _clear_other_calls(self):
        clear_index = []
        for i, obj in enumerate(self.context):
            if obj["role"] != "user":
                clear_index.append(i)
        self.context = [
            obj for i, obj in enumerate(self.context) if i not in clear_index
        ]

    def _remove_astrbot_system_reminder(
        self, ori_str: str
    ) -> str:  # 去除astrbot的系统提示<system_reminder>
        if ori_str.find("<system_reminder>") != -1:
            return ori_str[: ori_str.find("<system_reminder>")]
        else:
            return ori_str

    def parse_context(self, role_lenth: int) -> str:
        """将最近的几轮对话整理成可读的字符串形式
        Args:
            role_lenth(int):需要保留的轮数
        Returns:
            parsed_str(str):格式化后的字符串
        """
        self._clear_other_calls()
        self.context = self.context[max(0, len(self.context) - role_lenth) :]
        parsed_str = ""
        for i, context_obj in enumerate(self.context):
            text = ""
            if isinstance(context_obj["content"], str):
                text = context_obj["content"]
                text = self._remove_astrbot_system_reminder(text)
            else:
                for j, son_obj in enumerate(context_obj["content"]):
                    if son_obj["type"] == "image_url":
                        text += "[图片]"
                    elif son_obj["type"] == "input_audio":
                        text += "[语音]"
                    elif son_obj["type"] == "file":
                        text += "[文件]"
                    elif son_obj.get("text"):
                        text += son_obj["text"]
                        text = self._remove_astrbot_system_reminder(text)
            parsed_str += f"\n[Round{i + 1}]{text}"
        return parsed_str.strip()
