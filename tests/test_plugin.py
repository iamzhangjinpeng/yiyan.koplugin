"""Offline regression checks. Run: python -m unittest discover -s tests

Requires lupa (Lua 5.1); all KOReader/device/network APIs are replaced with
fixtures. No Hitokoto requests or Kindle files are touched.
"""
import json
from pathlib import Path
import tempfile
import unittest

from lupa.lua51 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture" * 4 + b"\0\0\0\0IEND\xaeB`\x82"

MOCKS = r'''
f = { queue = {}, requests = 0, renders = 0, live_widgets = 0, locks = 0,
    paints = {}, now = 0, connected = true, settings = { data = {} } }
function f.settings:readSetting(k) return self.data[k] end
function f.settings:saveSetting(k, v) self.data[k] = v end
function f.settings:delSetting(k) self.data[k] = nil end
function f.settings:flush() end
G_reader_settings = f.settings
local ui = {}
function ui:scheduleIn(_, cb) table.insert(f.queue, cb) end
function ui:unschedule(cb)
    for i = #f.queue, 1, -1 do if f.queue[i] == cb then table.remove(f.queue, i) end end
end
function ui:show(message) f.message = message.text end
function ui:preventStandby() f.locks = f.locks + 1 end
function ui:allowStandby() f.locks = f.locks - 1 end
function f:drain()
    local steps = 0
    while #self.queue > 0 do
        steps = steps + 1
        assert(steps < 100, "scheduler failed to settle")
        table.remove(self.queue, 1)()
    end
    assert(self.locks == 0, "standby lock leaked")
end
local bb = {}
function bb.new(w, h)
    local image = { w = w, h = h }
    function image:getWidth() return self.w end
    function image:getHeight() return self.h end
    function image:free() assert(not self.freed); self.freed = true end
    function image:blitFrom() end
    function image:ditherblitFrom() self.dithered = true end
    function image:paintRect(_, y, _, h) assert(y + h == self.h) end
    function image:writeToFile(path, format, _, grayscale)
        assert(format == "png" and grayscale == true)
        assert(self.dithered, "export must use 16-tone e-ink dithering")
        if f.fail_render then return false end
        local out = assert(io.open(path, "wb")); out:write(test_png); out:close()
        f.renders = f.renders + 1
        return true
    end
    return image
end
bb.TYPE_BB8, bb.COLOR_BLACK, bb.COLOR_WHITE = 1, "black", "white"
local text = {}
function text:new(t)
    f.live_widgets = f.live_widgets + 1
    function t:getSize()
        local rows = 0
        for line in (self.text .. "\n"):gmatch("(.-)\n") do
            local chars = 0
            for i = 1, #line do
                local b = line:byte(i)
                if b < 128 or b >= 192 then chars = chars + 1 end
            end
            rows = rows + math.max(1, math.ceil(chars * self.face.size / self.width))
        end
        return { w = self.width, h = self.height or math.ceil(rows * self.face.size * (1 + self.line_height)) }
    end
    function t:paintTo(_, x, y)
        assert(self:getSize().h > 0)
        table.insert(f.paints, { x = x, y = y, h = self:getSize().h,
            fg = self.fgcolor, bg = self.bgcolor, text = self.text })
    end
    function t:free() assert(not self.freed); self.freed = true; f.live_widgets = f.live_widgets - 1 end
    return t
end
local ffi = {}
function ffi.runInSubProcess(task)
    f.done = not f.hung
    task(1, 1)
    return 1, 1
end
function ffi.writeToFD(_, value) f.pipe = value end
function ffi.readAllFromFD() return f.pipe end
function ffi.isSubProcessDone() return f.done end
function ffi.terminateSubProcess() f.done = true; f.kills = (f.kills or 0) + 1 end
function ffi.copyFile(a, b)
    local src = assert(io.open(a, "rb")); local data = src:read("*a"); src:close()
    local out = assert(io.open(b, "wb")); out:write(data); out:close(); return true
end
local http = {}
function http.request(request)
    f.requests = f.requests + 1
    assert(request.redirect == false, "a redirect must not spend another request")
    if f.http_error then return nil, "offline" end
    local ok, err = request.sink(f.raw_response)
    if not ok then return nil, err end
    return 1, f.http_code or 200
end
local modules = {
    ["ui/widget/container/widgetcontainer"] = { extend = function(_, t) return t end },
    ["ui/uimanager"] = ui,
    ["ui/widget/infomessage"] = { new = function(_, t) return t end },
    ["ui/widget/inputdialog"] = {},
    ["ui/widget/textboxwidget"] = text,
    ["ui/font"] = { getFace = function(_, _, size) return { size = size } end },
    ["ffi/blitbuffer"] = bb,
    ["ui/renderimage"] = {
        renderImageFile = function(_, path)
            assert(path == test_root .. "/assets/lyrics-background.png")
            if f.missing_background then return nil end
            return bb.new(758, 1024)
        end,
        scaleBlitBuffer = function(_, image, w, h) image:free(); return bb.new(w, h) end,
    },
    ["device"] = { screen = { getWidth = function() return 758 end, getHeight = function() return 1024 end } },
    ["util"] = { makePath = py_mkdir },
    ["ffi/util"] = ffi,
    ["socket.http"] = http,
    ["socketutil"] = { set_timeout = function() end },
    ["ui/network/manager"] = { isConnected = function() return f.connected end },
    ["ui/time"] = { to_s = function(v) return v end, boottime_or_monotonic = function() return f.now end },
    ["logger"] = { warn = function() end },
    ["json"] = { encode = py_encode, decode = py_decode },
    ["gettext"] = function(v) return v end,
    ["libs/libkoreader-lfs"] = {
        attributes = py_mode,
        dir = function(path)
            local names, i = py_listdir(path), 0
            return function() i = i + 1; return names[i] end
        end,
    },
}
for name, module in pairs(modules) do package.loaded[name] = module end
local input = assert(io.open(test_root .. "/main.lua", "rb"))
local source = input:read("*a"); input:close()
source = source:gsub('local SAVE_DIR = "[^"]+"', 'local SAVE_DIR = ' .. string.format("%q", test_dir .. "/"))
plugin = assert(loadstring(source, "@" .. test_root .. "/main.lua"))()
function newPlugin()
    return setmetatable({ path = test_root, ui = { menu = { registerToMainMenu = function() end } } }, { __index = plugin })
end
obj = newPlugin()
wallpaper = dofile(test_root .. "/wallpaper.lua")
'''


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiyan-check-")
        self.path = Path(self.tmp.name)
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        g = self.lua.globals()
        g.test_root, g.test_dir = str(ROOT), str(self.path)
        g.test_png = PNG
        g.py_mkdir = lambda p: Path(p).mkdir(parents=True, exist_ok=True)
        g.py_mode = lambda p, _: "directory" if Path(p).is_dir() else "file" if Path(p).is_file() else None
        g.py_listdir = lambda p: self.lua.table_from(sorted(x.name for x in Path(p).iterdir()))
        g.py_encode = lambda v: json.dumps(self.to_python(v), ensure_ascii=False)
        g.py_decode = lambda s: self.to_lua(json.loads(s))
        self.lua.execute(MOCKS)
        self.f, self.obj = g.f, g.obj
        self.payload = {"hitokoto": "用代码表达言语的魅力，用代码书写山河的壮丽。", "from": "一言开发者中心", "from_who": "一言"}
        self.f.raw_response = json.dumps(self.payload, ensure_ascii=False)
        self.date = self.lua.eval('os.date("!%Y%m%d", os.time() + 8 * 3600)')

    def tearDown(self):
        self.tmp.cleanup()

    def to_python(self, value):
        if hasattr(value, "items"):
            return {k: self.to_python(v) for k, v in value.items()}
        return value

    def to_lua(self, value):
        return self.lua.table_from({k: self.to_lua(v) for k, v in value.items()}) if isinstance(value, dict) else value

    def refresh(self, force=False, cache_only=False):
        self.obj.refreshDaily(self.obj, True, force, cache_only)
        self.f.drain(self.f)

    def cache(self, version=None):
        record = {"date": self.date, "payload": self.payload}
        if version is not None: record["layout_version"] = version
        (self.path / (self.date + ".json")).write_text(json.dumps(record, ensure_ascii=False))

    def test_success_is_requested_only_once_even_after_settings_reset(self):
        self.refresh()
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertEqual(json.loads((self.path / (self.date + ".json")).read_text())["layout_version"], 2)
        self.f.settings.data = self.lua.table()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertEqual(self.f.live_widgets, 0)

    def test_failed_request_is_not_retried_after_restart(self):
        self.f.http_error = True
        self.refresh()
        self.f.settings.data = self.lua.table()
        self.obj = self.lua.globals().newPlugin()
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_offline_check_does_not_consume_daily_attempt(self):
        self.f.connected = False
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertFalse((self.path / (self.date + ".attempt")).exists())
        self.f.connected = True
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_failed_render_can_be_rebuilt_offline_without_refetch(self):
        self.f.fail_render = True
        self.refresh()
        self.assertTrue((self.path / (self.date + ".json")).exists())
        self.f.fail_render, self.f.connected = False, False
        self.refresh(True, True)
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_template_upgrade_uses_cached_sentence(self):
        self.cache(1)
        (self.path / (self.date + ".png")).write_bytes(PNG)
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertEqual(self.f.renders, 1)

    def test_legacy_image_without_sentence_never_causes_another_request(self):
        (self.path / (self.date + ".png")).write_bytes(PNG)
        self.refresh(True)
        self.assertEqual(self.f.requests, 0)
        self.assertEqual(self.f.renders, 0)

    def test_footer_has_white_glyphs_on_black_and_body_stays_above_stones(self):
        self.cache()
        self.refresh()
        body, source, date = [self.f.paints[i] for i in (1, 2, 3)]
        self.assertEqual((body.bg, body.fg), ("white", "black"))
        self.assertLess(body.y + body.h, 700)
        self.assertEqual((source.bg, source.fg), ("black", "white"))
        self.assertEqual(source.text, "一言 · 一言开发者中心")
        self.assertGreaterEqual(source.y, 922)
        self.assertLessEqual(date.y + date.h, 1024)

    def test_oversized_source_is_kept_inside_footer(self):
        self.payload["from"] = "很长的出处" * 80
        self.cache()
        self.refresh()
        source = self.f.paints[2]
        self.assertLessEqual(source.h, 47)
        self.assertLess(source.y + source.h, self.f.paints[3].y)

    def test_missing_artwork_preserves_previous_png_and_cached_sentence(self):
        target = self.path / (self.date + ".png")
        target.write_bytes(PNG)
        self.cache(1)
        self.f.missing_background = True
        self.refresh()
        self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(self.f.requests, 0)

    def test_large_response_never_goes_through_the_status_pipe(self):
        payload = dict(self.payload, **{"from": "a" * 10000})
        self.f.raw_response = json.dumps(payload, ensure_ascii=False)
        self.refresh()
        self.assertEqual(self.f.pipe, "ok")
        self.assertEqual(self.f.requests, 1)

    def test_excessive_response_is_rejected_without_retry(self):
        self.f.raw_response = "x" * 70000
        self.refresh()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertFalse((self.path / (self.date + ".png")).exists())

    def test_worker_timeout_is_reaped_and_releases_standby_lock(self):
        self.f.hung = True
        self.obj.refreshDaily(self.obj, True)
        self.f.now = 25
        self.f.drain(self.f)
        self.assertEqual(self.f.kills, 1)
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_ui_transition_cancels_previous_periodic_callback(self):
        self.f.settings.data["yiyan_auto_screensaver"] = False
        self.obj.init(self.obj)
        old = self.obj
        self.obj = self.lua.globals().newPlugin()
        self.obj.init(self.obj)
        self.assertTrue(old._stopped)
        self.assertEqual(len(self.f.queue), 1)


if __name__ == "__main__":
    unittest.main()
