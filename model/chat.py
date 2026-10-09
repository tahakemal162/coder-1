"""coder-1 sohbet formatı — kendi ChatML-benzeri şablonumuz.

<|im_start|>system\n...<|im_end|>\n<|im_start|>user\n...<|im_end|>\n<|im_start|>assistant\n

Araç çağrısı asistan mesajında: <|tool|>{"name":..,"args":{..}}
Araç sonucu rolü: <|im_start|>tool\n<|tool_result|>...<|im_end|>
"""


def render_chat(messages, add_generation_prompt=True):
    """messages: [{role, content}] → prompt metni."""
    out = []
    for m in messages:
        out.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n")
    if add_generation_prompt:
        out.append("<|im_start|>assistant\n")
    return "".join(out)


SYSTEM_CODER = (
    "Sen coder-1'sun: kullanıcının bilgisayarında kod yazan ve komut çalıştıran "
    "bir yapay zeka ajanısın. Görev için gerekiyorsa araç çağır: "
    '<|tool|>{"name":"ARAC","args":{...}} şeklinde tek satır JSON yaz. '
    "Araç sonucu sana <|tool_result|> ile döner. Görev bitince <|done|> ile kapanır."
)
