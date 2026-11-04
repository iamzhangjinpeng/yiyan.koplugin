"""Offline regression checks. Run: python -m unittest discover -s tests

Requires lupa (Lua 5.1); all KOReader/device/network APIs are replaced with
fixtures. No Hitokoto requests or Kindle files are touched.
"""
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from lupa.lua51 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
PNG = (ROOT / "assets/lyrics-background.png").read_bytes()
BROKEN_PNG = b"\x89PNG\r\n\x1a\n" + b"fixture" * 4 + b"\0\0\0\0IEND\xaeB`\x82"

MOCKS = r'''
f = { queue = {}, requests = 0, renders = 0, live_widgets = 0, locks = 0,
    paints = {}, shapes = {}, now = 0, connected = true, settings = { data = {} } }
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
    function image:paintRect(x, y, w, h, color)
        assert(x >= 0 and y >= 0 and w > 0 and h > 0)
        assert(x + w <= self.w and y + h <= self.h)
        table.insert(f.shapes, { kind = "rect", x = x, y = y, w = w, h = h, color = color })
    end
    function image:paintCircle(x, y, radius, color)
        assert(x - radius >= 0 and y - radius >= 0 and x + radius < self.w and y + radius < self.h)
        table.insert(f.shapes, { kind = "circle", x = x, y = y, radius = radius, color = color })
    end
    function image:writeToFile(path, format)
        assert(format == "png")
        assert(self.dithered, "export must use 16-tone e-ink dithering")
        if f.fail_render then return false end
        local out = assert(io.open(path, "wb"))
        out:write(f.invalid_png or (f.truncate_png and "truncated" or test_png))
        out:close()
        if f.fail_encode then return true, false, "disk full" end
        f.renders = f.renders + 1
        f.export_width, f.export_height = self.w, self.h
        return true, true
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
        table.insert(f.paints, { x = x, y = y, w = self.width, h = self:getSize().h,
            size = self.face.size, bold = self.bold, line_height = self.line_height,
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
    ["ui/font"] = { getFace = function(_, _, size)
        return { size = math.ceil(size * (f.screen_scale or 1)) }
    end },
    ["ffi/blitbuffer"] = bb,
    ["ffi"] = { C = { free = function(data) assert(data == "decoded") end } },
    ["ffi/png"] = { decodeFromFile = function(path, components)
        assert(components == 1)
        local file = io.open(path, "rb")
        if not file then return false end
        local data = file:read("*a")
        file:close()
        if data == test_png then return true, { data = "decoded" } end
        return false, "invalid PNG"
    end },
    ["ui/renderimage"] = {
        renderImageFile = function(_, path)
            assert(path == test_root .. "/assets/lyrics-background.png")
            if f.missing_background then return nil end
            return bb.new(758, 1024)
        end,
        scaleBlitBuffer = function(_, image, w, h) image:free(); return bb.new(w, h) end,
    },
    ["device"] = { screen = {
        getWidth = function() return f.screen_width or 758 end,
        getHeight = function() return f.screen_height or 1024 end,
        scaleBySize = function(_, size) return math.ceil(size * (f.screen_scale or 1)) end,
    } },
    ["util"] = { makePath = function(path) py_mkdir(path); return true end },
    ["ffi/util"] = ffi,
    ["socket.http"] = http,
    ["socketutil"] = { set_timeout = function() end },
    ["ui/network/manager"] = { isConnected = function() return f.connected end },
    ["ui/time"] = { to_s = function(v) return v end, boottime_or_monotonic = function() return f.now end },
    ["logger"] = { warn = function() end },
    ["json"] = { encode = py_encode, decode = py_decode },
    ["ffi/sha2"] = { md5 = function(data)
        return (data:gsub(".", function(c) return string.format("%02x", c:byte()) end))
    end },
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
        self.assertEqual(json.loads((self.path / (self.date + ".json")).read_text())["layout_version"], 4)
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
        self.assertEqual(source.text, "一言开发者中心")
        self.assertTrue(source.bold)
        self.assertEqual(date.text, f"一言  ·  {self.date[:4]}-{self.date[4:6]}-{self.date[6:]}")
        self.assertGreaterEqual(source.y, 922)
        self.assertLessEqual(date.y + date.h, 1024)

    def test_oversized_source_is_kept_inside_footer(self):
        self.payload["from"] = "很长的出处" * 80
        self.cache()
        self.refresh()
        source = self.f.paints[2]
        self.assertLessEqual(source.h, 41)
        self.assertLess(source.y + source.h, self.f.paints[3].y)

    def test_cassette_and_title_fit_on_a_high_resolution_screen(self):
        self.f.screen_width, self.f.screen_height, self.f.screen_scale = 1440, 1920, 2.4
        self.payload.update({"from": "一次长途旅行", "from_who": "作者"})
        self.cache()
        self.refresh()
        footer, cassette = self.f.shapes[1], self.f.shapes[2]
        body, title, metadata = [self.f.paints[i] for i in (1, 2, 3)]
        self.assertEqual((body.x, cassette.x), (cassette.x, 161))
        self.assertGreaterEqual(cassette.y, footer.y)
        self.assertLess(cassette.x + cassette.w, title.x)
        self.assertEqual(title.text, "一次长途旅行")
        self.assertTrue(metadata.text.startswith("作者  ·  "))
        self.assertLess(title.y + title.h, metadata.y)
        self.assertLessEqual(metadata.y + metadata.h, 1920)
        self.assertLessEqual(title.size, 40)

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

    def test_cleanup_checks_ownership_and_leaves_replaced_images(self):
        self.cache()
        records = {
            "20000101": {},
            "20000102": {"owner": "yiyan.koplugin", "image_digest": PNG.hex()},
            "20000103": {"owner": "yiyan.koplugin", "image_digest": PNG.hex()},
        }
        for date, flags in records.items():
            (self.path / (date + ".json")).write_text(json.dumps({
                "date": date, "payload": self.payload, **flags,
            }))
            (self.path / (date + ".png")).write_bytes(PNG if date != "20000103" else b"user image")
        self.refresh()
        self.assertTrue((self.path / "20000101.png").exists())
        self.assertTrue((self.path / "20000101.json").exists())
        self.assertFalse((self.path / "20000102.png").exists())
        self.assertFalse((self.path / "20000102.json").exists())
        self.assertEqual((self.path / "20000103.png").read_bytes(), b"user image")
        display = self.path / ".yiyan-screen"
        self.assertEqual([p.name for p in display.iterdir()], ["wallpaper.png"])

    def test_encoder_error_or_truncation_preserves_previous_png(self):
        target = self.path / (self.date + ".png")
        target.write_bytes(PNG)
        self.cache(1)
        self.f.truncate_png = True
        for encoder_error in (True, False):
            with self.subTest(encoder_error=encoder_error):
                self.f.fail_encode = encoder_error
                self.refresh()
                self.assertEqual(target.read_bytes(), PNG)
                self.assertFalse(target.with_suffix(".png.tmp").exists())

    def test_offline_start_keeps_original_screensaver_until_image_is_ready(self):
        self.f.connected = False
        self.f.settings.data["screensaver_type"] = "cover"
        self.obj.init(self.obj)
        self.obj.refreshDaily(self.obj, True)
        self.assertEqual(self.f.settings.data["screensaver_type"], "cover")
        self.assertIsNone(self.f.settings.data["yiyan_screensaver_backup"])
        self.cache()
        self.obj.refreshDaily(self.obj, True)
        self.assertEqual(self.f.settings.data["screensaver_type"], "random_image")
        self.assertEqual(self.f.settings.data["screensaver_dir"], str(self.path / ".yiyan-screen") + "/")
        self.obj.stopPlugin(self.obj)
        self.assertEqual(self.f.settings.data["screensaver_type"], "cover")

    def test_landscape_reader_renders_portrait_wallpaper(self):
        self.cache(2)
        (self.path / (self.date + ".png")).write_bytes(PNG)
        self.f.screen_width, self.f.screen_height = 1024, 758
        self.refresh()
        self.assertEqual((self.f.export_width, self.f.export_height), (758, 1024))
        self.assertEqual(self.f.requests, 0)

    def test_directory_save_waits_for_active_request(self):
        # The timer can start a request while the directory dialog is open.
        new_path = self.path / "new-output"
        self.lua.globals().test_next_dir = str(new_path)
        self.lua.execute(r'''
            local function map(path)
                local prefix = "/mnt/us/test/"
                if path:sub(1, #prefix) == prefix then
                    return test_next_dir .. "/" .. path:sub(#prefix + 1)
                end
                return path
            end
            local open, rename, remove = io.open, os.rename, os.remove
            io.open = function(path, ...) return open(map(path), ...) end
            os.rename = function(src, dst) return rename(map(src), map(dst)) end
            os.remove = function(path) return remove(map(path)) end
            package.loaded["util"].makePath = function(path)
                py_mkdir(map(path))
                return true
            end
            local ui = package.loaded["ui/uimanager"]
            ui.close = function() end
            package.loaded["ui/widget/inputdialog"].new = function(_, options)
                options.getInputText = function() return "/mnt/us/test/" end
                options.onShowKeyboard = function() end
                pending_save = options.buttons[1][2].callback
                return options
            end
            obj:chooseOutputDirectory()
            obj:refreshDaily(true)
            pending_save()
        ''')
        self.assertIsNone(self.f.settings.data["yiyan_save_dir"])
        self.assertIn("请等待", self.f.message)
        self.f.drain(self.f)
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertTrue((self.path / (self.date + ".json")).exists())
        self.lua.execute("pending_save()")
        self.assertEqual(self.f.settings.data["yiyan_save_dir"], "/mnt/us/test/")
        for suffix in (".png", ".json", ".attempt"):
            name = self.date + suffix
            self.assertEqual((new_path / name).read_bytes(), (self.path / name).read_bytes())
        self.assertEqual((new_path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_foreign_cache_is_preserved_before_and_during_request(self):
        path = self.path / (self.date + ".json")
        original = b'{"application":"another plugin","data":"personal notes"}'
        path.write_bytes(original)
        self.refresh()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.f.requests, 0)
        self.assertFalse(path.with_suffix(".attempt").exists())

        path.unlink()
        self.obj.refreshDaily(self.obj, True)
        path.write_bytes(original)
        self.f.drain(self.f)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.f.requests, 1)
        self.assertFalse(path.with_suffix(".png").exists())

    def test_rebuild_does_not_overwrite_user_replacement(self):
        self.refresh()
        path = self.path / (self.date + ".png")
        path.write_bytes(b"user replacement")
        self.refresh(True, True)
        self.assertEqual(path.read_bytes(), b"user replacement")
        self.assertEqual(self.f.requests, 1)

    def test_undecodable_png_with_valid_header_and_tail_is_never_published(self):
        display = self.path / ".yiyan-screen/wallpaper.png"
        display.parent.mkdir()
        display.write_bytes(PNG)
        archive = self.path / (self.date + ".png")
        archive.write_bytes(BROKEN_PNG)
        self.refresh()
        self.assertEqual(display.read_bytes(), PNG)
        self.assertEqual(archive.read_bytes(), BROKEN_PNG)
        self.assertEqual(self.f.requests, 0)

        archive.write_bytes(PNG)
        self.cache(2)
        self.f.invalid_png = BROKEN_PNG
        self.refresh()
        self.assertEqual(archive.read_bytes(), PNG)
        self.assertEqual(display.read_bytes(), PNG)

    def test_periodic_refresh_and_disable_respect_manual_screensaver_change(self):
        settings = self.f.settings.data
        settings["screensaver_type"] = "cover"
        self.refresh()
        self.assertEqual(settings["screensaver_type"], "random_image")
        settings["screensaver_type"] = "readingprogress"
        settings["screensaver_dir"] = "/mnt/us/my-wallpapers/"
        self.refresh()
        self.obj.stopPlugin(self.obj)
        self.assertEqual(settings["screensaver_type"], "readingprogress")
        self.assertEqual(settings["screensaver_dir"], "/mnt/us/my-wallpapers/")
        self.assertIsNone(settings["yiyan_screensaver_backup"])

    def test_request_rechecks_beijing_date_inside_worker(self):
        self.lua.globals().review_clock = int(datetime(2026, 11, 4, 15, 59, 59, tzinfo=timezone.utc).timestamp())
        self.lua.execute(r'''
            os.time = function() return review_clock end
            local ffi = package.loaded["ffi/util"]
            local spawn = ffi.runInSubProcess
            ffi.runInSubProcess = function(...)
                review_clock = review_clock + 1
                return spawn(...)
            end
        ''')
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / "20261105.png").exists())
        self.refresh()
        self.assertEqual(self.f.requests, 1)


if __name__ == "__main__":
    unittest.main()
