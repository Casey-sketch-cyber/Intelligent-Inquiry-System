"""供应商身份判定：有公司资料时必须一致；缺少资料时只接受完整字号。"""
import re
import unicodedata
import ctypes

REGIONS = r"深圳市|深圳|香港|上海|北京|广州|苏州|杭州|中国"
SUFFIXES = r"股份有限公司|有限责任公司|有限公司|微电子|半导体|公司|股份|集团|科技|电子|实业|贸易|国际|技术|发展|商行"


class SupplierIdentityError(RuntimeError):
    pass


class SendUncertainError(RuntimeError):
    """已点击发送但无法确认结果，不能自动重发。"""


def core_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    if value:
        try:
            mapper=ctypes.windll.kernel32.LCMapStringEx
            size=mapper("zh-CN",0x02000000,value,-1,None,0,None,None,0)
            if size:
                buffer=ctypes.create_unicode_buffer(size)
                if mapper("zh-CN",0x02000000,value,-1,buffer,size,None,None,0): value=buffer.value
        except (AttributeError,OSError): pass
    value=re.sub(r"(有限公司|有限责任公司)\(原[^()]{2,40}\)$",r"\1",value)
    value=re.sub(r"(有限公司|有限责任公司)销售[一二三四五六七八九十0-9]+部$",r"\1",value)
    value = re.sub(rf"\((?:{REGIONS})\)", "", value)
    value = re.sub(rf"^(?:(?:{REGIONS}))+", "", value)
    # 只去掉地区前缀和组织后缀，不能删除字号中间的“科技/电子”。
    while True:
        shorter = re.sub(rf"(?:{SUFFIXES})$", "", value).strip()
        if shorter == value:
            return value
        value = shorter


def identity_error(company: str, chat_name: str | None, profile_company: str | None) -> str | None:
    core = core_name(company)
    if len(core) < 2:
        return "供应商公司名不能可靠识别"
    if profile_company and profile_company.strip():
        if core_name(profile_company) == core:
            return None
        return f"资料卡公司「{profile_company}」与供应商「{company}」不一致"
    # 不允许短字号匹配较长字号，例如“恒佳”不能匹配“恒佳达”。
    tokens = [core_name(token) for token in re.split(r"[^\w\u4e00-\u9fff]+|_+", core_name(chat_name or ""))]
    if core in tokens:
        return None
    return f"没有匹配的公司资料，会话「{chat_name or '未知'}」无法核实为「{company}」"


def name_matches(company: str, chat_name: str | None, profile_company: str | None = None) -> bool:
    return identity_error(company, chat_name, profile_company) is None
