"""librime C API 的 ctypes 绑定。

按 third_party/librime-src/src/rime_api.h（master，与 rime.dll 0.17.4 对应）
完整转写。结构体字段顺序必须与头文件严格一致。
"""

import ctypes as ct
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_double,
                    c_int, c_size_t, c_void_p)

BOOL = c_int           # rime 的 Bool 就是 int
SESSION = c_size_t     # RimeSessionId = uintptr_t
NOTIFICATION_HANDLER = CFUNCTYPE(None, c_void_p, SESSION, c_char_p, c_char_p)


# ---------------------------------------------------------------- 结构体

class RimeTraits(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("shared_data_dir", c_char_p),
        ("user_data_dir", c_char_p),
        ("distribution_name", c_char_p),
        ("distribution_code_name", c_char_p),
        ("distribution_version", c_char_p),
        ("app_name", c_char_p),
        ("modules", POINTER(c_char_p)),
        ("min_log_level", c_int),
        ("log_dir", c_char_p),
        ("prebuilt_data_dir", c_char_p),
        ("staging_dir", c_char_p),
    ]


class RimeComposition(Structure):
    _fields_ = [
        ("length", c_int),
        ("cursor_pos", c_int),
        ("sel_start", c_int),
        ("sel_end", c_int),
        ("preedit", c_char_p),
    ]


class RimeCandidate(Structure):
    _fields_ = [
        ("text", c_char_p),
        ("comment", c_char_p),
        ("reserved", c_void_p),
    ]


class RimeMenu(Structure):
    _fields_ = [
        ("page_size", c_int),
        ("page_no", c_int),
        ("is_last_page", BOOL),
        ("highlighted_candidate_index", c_int),
        ("num_candidates", c_int),
        ("candidates", POINTER(RimeCandidate)),
        ("select_keys", c_char_p),
    ]


class RimeCommit(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("text", c_char_p),
    ]


class RimeContext(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("composition", RimeComposition),
        ("menu", RimeMenu),
        ("commit_text_preview", c_char_p),
        ("select_labels", POINTER(c_char_p)),
    ]


class RimeCandidatePreview(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("text_before_selection", c_char_p),
        ("selected_text", c_char_p),
        ("text_after_selection", c_char_p),
    ]


class RimeStatus(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("schema_id", c_char_p),
        ("schema_name", c_char_p),
        ("is_disabled", BOOL),
        ("is_composing", BOOL),
        ("is_ascii_mode", BOOL),
        ("is_full_shape", BOOL),
        ("is_simplified", BOOL),
        ("is_traditional", BOOL),
        ("is_ascii_punct", BOOL),
    ]


class RimeCandidateListIterator(Structure):
    _fields_ = [
        ("ptr", c_void_p),
        ("index", c_int),
        ("candidate", RimeCandidate),
    ]


class RimeConfig(Structure):
    _fields_ = [("ptr", c_void_p)]


class RimeConfigIterator(Structure):
    _fields_ = [
        ("list", c_void_p),
        ("map", c_void_p),
        ("index", c_int),
        ("key", c_char_p),
        ("path", c_char_p),
    ]


class RimeSchemaListItem(Structure):
    _fields_ = [
        ("schema_id", c_char_p),
        ("name", c_char_p),
        ("reserved", c_void_p),
    ]


class RimeSchemaList(Structure):
    _fields_ = [
        ("size", c_size_t),
        ("list", POINTER(RimeSchemaListItem)),
    ]


class RimeStringSlice(Structure):
    _fields_ = [
        ("str", c_char_p),
        ("length", c_size_t),
    ]


class RimeCustomApi(Structure):
    _fields_ = [("data_size", c_int)]


class RimeModule(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("module_name", c_char_p),
        ("initialize", c_void_p),
        ("finalize", c_void_p),
        ("get_api", c_void_p),
    ]


# ---------------------------------------------------------------- API 表

class RimeApi(Structure):
    _fields_ = [
        ("data_size", c_int),
        ("setup", CFUNCTYPE(None, POINTER(RimeTraits))),
        ("set_notification_handler", CFUNCTYPE(None, NOTIFICATION_HANDLER, c_void_p)),
        ("initialize", CFUNCTYPE(None, POINTER(RimeTraits))),
        ("finalize", CFUNCTYPE(None)),
        ("start_maintenance", CFUNCTYPE(BOOL, BOOL)),
        ("is_maintenance_mode", CFUNCTYPE(BOOL)),
        ("join_maintenance_thread", CFUNCTYPE(None)),
        ("deployer_initialize", CFUNCTYPE(None, POINTER(RimeTraits))),
        ("prebuild", CFUNCTYPE(BOOL)),
        ("deploy", CFUNCTYPE(BOOL)),
        ("deploy_schema", CFUNCTYPE(BOOL, c_char_p)),
        ("deploy_config_file", CFUNCTYPE(BOOL, c_char_p, c_char_p)),
        ("sync_user_data", CFUNCTYPE(BOOL)),
        ("create_session", CFUNCTYPE(SESSION)),
        ("find_session", CFUNCTYPE(BOOL, SESSION)),
        ("destroy_session", CFUNCTYPE(BOOL, SESSION)),
        ("cleanup_stale_sessions", CFUNCTYPE(None)),
        ("cleanup_all_sessions", CFUNCTYPE(None)),
        ("process_key", CFUNCTYPE(BOOL, SESSION, c_int, c_int)),
        ("commit_composition", CFUNCTYPE(BOOL, SESSION)),
        ("clear_composition", CFUNCTYPE(None, SESSION)),
        ("get_commit", CFUNCTYPE(BOOL, SESSION, POINTER(RimeCommit))),
        ("free_commit", CFUNCTYPE(BOOL, POINTER(RimeCommit))),
        ("get_context", CFUNCTYPE(BOOL, SESSION, POINTER(RimeContext))),
        ("free_context", CFUNCTYPE(BOOL, POINTER(RimeContext))),
        ("get_status", CFUNCTYPE(BOOL, SESSION, POINTER(RimeStatus))),
        ("free_status", CFUNCTYPE(BOOL, POINTER(RimeStatus))),
        ("set_option", CFUNCTYPE(None, SESSION, c_char_p, BOOL)),
        ("get_option", CFUNCTYPE(BOOL, SESSION, c_char_p)),
        ("set_property", CFUNCTYPE(None, SESSION, c_char_p, c_char_p)),
        ("get_property", CFUNCTYPE(BOOL, SESSION, c_char_p, c_char_p, c_size_t)),
        ("get_schema_list", CFUNCTYPE(BOOL, POINTER(RimeSchemaList))),
        ("free_schema_list", CFUNCTYPE(None, POINTER(RimeSchemaList))),
        ("get_current_schema", CFUNCTYPE(BOOL, SESSION, c_char_p, c_size_t)),
        ("select_schema", CFUNCTYPE(BOOL, SESSION, c_char_p)),
        ("schema_open", CFUNCTYPE(BOOL, c_char_p, POINTER(RimeConfig))),
        ("config_open", CFUNCTYPE(BOOL, c_char_p, POINTER(RimeConfig))),
        ("config_close", CFUNCTYPE(BOOL, POINTER(RimeConfig))),
        ("config_get_bool", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, POINTER(BOOL))),
        ("config_get_int", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, POINTER(c_int))),
        ("config_get_double", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, POINTER(c_double))),
        ("config_get_string", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, c_char_p, c_size_t)),
        ("config_get_cstring", CFUNCTYPE(c_char_p, POINTER(RimeConfig), c_char_p)),
        ("config_update_signature", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p)),
        ("config_begin_map", CFUNCTYPE(BOOL, POINTER(RimeConfigIterator), POINTER(RimeConfig), c_char_p)),
        ("config_next", CFUNCTYPE(BOOL, POINTER(RimeConfigIterator))),
        ("config_end", CFUNCTYPE(None, POINTER(RimeConfigIterator))),
        ("simulate_key_sequence", CFUNCTYPE(BOOL, SESSION, c_char_p)),
        ("register_module", CFUNCTYPE(BOOL, POINTER(RimeModule))),
        ("find_module", CFUNCTYPE(POINTER(RimeModule), c_char_p)),
        ("run_task", CFUNCTYPE(BOOL, c_char_p)),
        ("get_shared_data_dir", CFUNCTYPE(c_char_p)),
        ("get_user_data_dir", CFUNCTYPE(c_char_p)),
        ("get_sync_dir", CFUNCTYPE(c_char_p)),
        ("get_user_id", CFUNCTYPE(c_char_p)),
        ("get_user_data_sync_dir", CFUNCTYPE(None, c_char_p, c_size_t)),
        ("config_init", CFUNCTYPE(BOOL, POINTER(RimeConfig))),
        ("config_load_string", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p)),
        ("config_set_bool", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, BOOL)),
        ("config_set_int", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, c_int)),
        ("config_set_double", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, c_double)),
        ("config_set_string", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, c_char_p)),
        ("config_get_item", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, POINTER(RimeConfig))),
        ("config_set_item", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p, POINTER(RimeConfig))),
        ("config_clear", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p)),
        ("config_create_list", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p)),
        ("config_create_map", CFUNCTYPE(BOOL, POINTER(RimeConfig), c_char_p)),
        ("config_list_size", CFUNCTYPE(c_size_t, POINTER(RimeConfig), c_char_p)),
        ("config_begin_list", CFUNCTYPE(BOOL, POINTER(RimeConfigIterator), POINTER(RimeConfig), c_char_p)),
        ("get_input", CFUNCTYPE(c_char_p, SESSION)),
        ("get_caret_pos", CFUNCTYPE(c_size_t, SESSION)),
        ("select_candidate", CFUNCTYPE(BOOL, SESSION, c_size_t)),
        ("get_version", CFUNCTYPE(c_char_p)),
        ("set_caret_pos", CFUNCTYPE(None, SESSION, c_size_t)),
        ("select_candidate_on_current_page", CFUNCTYPE(BOOL, SESSION, c_size_t)),
        ("candidate_list_begin", CFUNCTYPE(BOOL, SESSION, POINTER(RimeCandidateListIterator))),
        ("candidate_list_next", CFUNCTYPE(BOOL, POINTER(RimeCandidateListIterator))),
        ("candidate_list_end", CFUNCTYPE(None, POINTER(RimeCandidateListIterator))),
        ("user_config_open", CFUNCTYPE(BOOL, c_char_p, POINTER(RimeConfig))),
        ("candidate_list_from_index", CFUNCTYPE(BOOL, SESSION, POINTER(RimeCandidateListIterator), c_int)),
        ("get_prebuilt_data_dir", CFUNCTYPE(c_char_p)),
        ("get_staging_dir", CFUNCTYPE(c_char_p)),
        ("commit_proto", CFUNCTYPE(None, SESSION, c_void_p)),
        ("context_proto", CFUNCTYPE(None, SESSION, c_void_p)),
        ("status_proto", CFUNCTYPE(None, SESSION, c_void_p)),
        ("get_state_label", CFUNCTYPE(c_char_p, SESSION, c_char_p, BOOL)),
        ("delete_candidate", CFUNCTYPE(BOOL, SESSION, c_size_t)),
        ("delete_candidate_on_current_page", CFUNCTYPE(BOOL, SESSION, c_size_t)),
        ("get_state_label_abbreviated", CFUNCTYPE(RimeStringSlice, SESSION, c_char_p, BOOL, BOOL)),
        ("set_input", CFUNCTYPE(BOOL, SESSION, c_char_p)),
        ("get_shared_data_dir_s", CFUNCTYPE(None, c_char_p, c_size_t)),
        ("get_user_data_dir_s", CFUNCTYPE(None, c_char_p, c_size_t)),
        ("get_prebuilt_data_dir_s", CFUNCTYPE(None, c_char_p, c_size_t)),
        ("get_staging_dir_s", CFUNCTYPE(None, c_char_p, c_size_t)),
        ("get_sync_dir_s", CFUNCTYPE(None, c_char_p, c_size_t)),
        ("highlight_candidate", CFUNCTYPE(BOOL, SESSION, c_size_t)),
        ("highlight_candidate_on_current_page", CFUNCTYPE(BOOL, SESSION, c_size_t)),
        ("change_page", CFUNCTYPE(BOOL, SESSION, BOOL)),
        ("get_candidate_preview", CFUNCTYPE(BOOL, SESSION, POINTER(RimeCandidatePreview))),
        ("free_candidate_preview", CFUNCTYPE(BOOL, POINTER(RimeCandidatePreview))),
    ]


# ---------------------------------------------------------------- 封装

def _b(s):
    """str/None -> bytes/None"""
    return s.encode("utf-8") if isinstance(s, str) else s


def _s(b):
    """bytes/None -> str/None"""
    return b.decode("utf-8", "replace") if b else None


class Candidate:
    __slots__ = ("text", "comment", "index")

    def __init__(self, text, comment, index):
        self.text = text
        self.comment = comment
        self.index = index

    def __repr__(self):
        return f"<Candidate {self.index}: {self.text} {self.comment or ''}>"


class Context:
    """一次 get_context 的快照。"""

    __slots__ = ("preedit", "caret_pos", "candidates", "highlighted",
                 "page_no", "page_size", "is_last_page", "commit_preview",
                 "select_labels")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Rime:
    """librime 的 Python 封装。用法：

    r = Rime(shared_data_dir=..., user_data_dir=...)
    r.initialize()                 # setup + initialize + 等待部署完成
    sid = r.create_session()
    r.process_key(sid, keycode, mask)
    ctx = r.get_context(sid)       # 取编码串和候选
    r.select_candidate(sid, 0)
    commit = r.get_commit(sid)     # 取上屏文本
    """

    def __init__(self, dll_path, shared_data_dir, user_data_dir,
                 app_name="rime.shangkey", distribution_name="Rime",
                 distribution_version="0.17.4", min_log_level=1,
                 log_dir=None):
        self._dll = ct.WinDLL(dll_path)
        self._dll.rime_get_api.restype = POINTER(RimeApi)
        self.api = self._dll.rime_get_api().contents
        self._notification_cb = None  # 防止回调被 GC
        self._traits = RimeTraits()
        t = self._traits
        t.data_size = ct.sizeof(RimeTraits) - ct.sizeof(c_int)
        t.shared_data_dir = _b(shared_data_dir)
        t.user_data_dir = _b(user_data_dir)
        t.distribution_name = _b(distribution_name)
        t.distribution_version = _b(distribution_version)
        t.app_name = _b(app_name)
        t.min_log_level = min_log_level
        t.log_dir = _b(log_dir)
        self._session = None

    # ---- 生命周期

    def initialize(self, on_notification=None, deploy_timeout=180):
        """setup + initialize + 部署。

        api.initialize 只加载 default 模块组（core/dict/gears）并启动服务；
        部署任务（installation_update/workspace_update 等）由 deployer 模块组
        （core/dict/levers）注册，start_maintenance 会加载该组并触发部署。
        """
        if on_notification is not None:
            self._notification_cb = NOTIFICATION_HANDLER(on_notification)
            self.api.set_notification_handler(self._notification_cb, None)
        self.api.setup(byref(self._traits))
        self.api.initialize(byref(self._traits))
        if not self.api.start_maintenance(True):
            raise RuntimeError("failed to start rime maintenance")
        import time
        deadline = time.time() + deploy_timeout
        while self.api.is_maintenance_mode():
            if time.time() > deadline:
                raise TimeoutError("rime deployment timed out")
            time.sleep(0.1)
        self.api.join_maintenance_thread()

    def finalize(self):
        if self._session is not None:
            self.destroy_session(self._session)
            self._session = None
        self.api.finalize()

    def version(self):
        return _s(self.api.get_version())

    def deploy(self):
        return bool(self.api.deploy())

    # ---- 会话

    def create_session(self):
        sid = self.api.create_session()
        self._session = sid
        return sid

    def destroy_session(self, sid):
        return bool(self.api.destroy_session(sid))

    @property
    def session(self):
        if self._session is None:
            self.create_session()
        return self._session

    # ---- 输入

    def process_key(self, keycode, mask=0, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.process_key(sid, keycode, mask))

    def simulate_key_sequence(self, seq, sid=None):
        """直接喂按键序列字符串，如 'nihao'。"""
        sid = sid if sid is not None else self.session
        return bool(self.api.simulate_key_sequence(sid, _b(seq)))

    def commit_composition(self, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.commit_composition(sid))

    def clear_composition(self, sid=None):
        sid = sid if sid is not None else self.session
        self.api.clear_composition(sid)

    def get_input(self, sid=None):
        sid = sid if sid is not None else self.session
        return _s(self.api.get_input(sid))

    def set_input(self, text, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.set_input(sid, _b(text)))

    # ---- 输出

    def get_commit(self, sid=None):
        sid = sid if sid is not None else self.session
        commit = RimeCommit()
        commit.data_size = ct.sizeof(RimeCommit) - ct.sizeof(c_int)
        if not self.api.get_commit(sid, byref(commit)):
            return None
        text = _s(commit.text)
        self.api.free_commit(byref(commit))
        return text

    def get_context(self, sid=None):
        sid = sid if sid is not None else self.session
        ctx = RimeContext()
        ctx.data_size = ct.sizeof(RimeContext) - ct.sizeof(c_int)
        if not self.api.get_context(sid, byref(ctx)):
            return None
        try:
            comp = ctx.composition
            menu = ctx.menu
            cands = []
            for i in range(menu.num_candidates):
                c = menu.candidates[i]
                cands.append(Candidate(_s(c.text), _s(c.comment), i))
            labels = []
            if ctx.select_labels:
                i = 0
                while ctx.select_labels[i]:
                    labels.append(_s(ctx.select_labels[i]))
                    i += 1
            return Context(
                preedit=_s(comp.preedit),
                caret_pos=comp.cursor_pos,
                candidates=cands,
                highlighted=menu.highlighted_candidate_index,
                page_no=menu.page_no,
                page_size=menu.page_size,
                is_last_page=bool(menu.is_last_page),
                commit_preview=_s(ctx.commit_text_preview),
                select_labels=labels,
            )
        finally:
            self.api.free_context(byref(ctx))

    def get_status(self, sid=None):
        sid = sid if sid is not None else self.session
        st = RimeStatus()
        st.data_size = ct.sizeof(RimeStatus) - ct.sizeof(c_int)
        if not self.api.get_status(sid, byref(st)):
            return None
        try:
            return {
                "schema_id": _s(st.schema_id),
                "schema_name": _s(st.schema_name),
                "is_composing": bool(st.is_composing),
                "is_ascii_mode": bool(st.is_ascii_mode),
                "is_simplified": bool(st.is_simplified),
                "is_traditional": bool(st.is_traditional),
                "is_ascii_punct": bool(st.is_ascii_punct),
            }
        finally:
            self.api.free_status(byref(st))

    # ---- 候选操作

    def select_candidate(self, index, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.select_candidate(sid, index))

    def select_candidate_on_current_page(self, index, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.select_candidate_on_current_page(sid, index))

    def highlight_candidate(self, index, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.highlight_candidate(sid, index))

    def change_page(self, backward=False, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.change_page(sid, backward))

    def get_candidate_preview(self, sid=None):
        sid = sid if sid is not None else self.session
        pv = RimeCandidatePreview()
        pv.data_size = ct.sizeof(RimeCandidatePreview) - ct.sizeof(c_int)
        if not self.api.get_candidate_preview(sid, byref(pv)):
            return None
        try:
            return {
                "before": _s(pv.text_before_selection),
                "selected": _s(pv.selected_text),
                "after": _s(pv.text_after_selection),
            }
        finally:
            self.api.free_candidate_preview(byref(pv))

    # ---- 模式 / 方案

    def set_option(self, option, value, sid=None):
        sid = sid if sid is not None else self.session
        self.api.set_option(sid, _b(option), 1 if value else 0)

    def get_option(self, option, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.get_option(sid, _b(option)))

    def set_ascii_mode(self, ascii_on, sid=None):
        """特色2 的挂接点：直接切换中/英模式。"""
        self.set_option("ascii_mode", ascii_on, sid)

    def get_ascii_mode(self, sid=None):
        st = self.get_status(sid if sid is not None else self.session)
        return bool(st and st["is_ascii_mode"])

    def select_schema(self, schema_id, sid=None):
        sid = sid if sid is not None else self.session
        return bool(self.api.select_schema(sid, _b(schema_id)))

    def get_schema_list(self):
        lst = RimeSchemaList()
        if not self.api.get_schema_list(byref(lst)):
            return []
        try:
            out = []
            for i in range(lst.size):
                item = lst.list[i]
                out.append((_s(item.schema_id), _s(item.name)))
            return out
        finally:
            self.api.free_schema_list(byref(lst))

    def sync_user_data(self):
        return bool(self.api.sync_user_data())
