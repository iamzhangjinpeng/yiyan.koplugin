-- Yiyan daily wallpaper for KOReader / Kindle.
-- Network I/O runs in a child process so it never blocks the reader UI.

local WidgetContainer = require("ui/widget/container/widgetcontainer")
local UIManager = require("ui/uimanager")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local Device = require("device")
local util = require("util")
local ffiutil = require("ffi/util")
local http = require("socket.http")
local socketutil = require("socketutil")
local NetworkMgr = require("ui/network/manager")
local logger = require("logger")
local lfs = require("libs/libkoreader-lfs")
local json = require("json")
local md5 = require("ffi/sha2").md5
local time = require("ui/time")
local _ = require("gettext")
local plugin_dir = debug.getinfo(1, "S").source:match("^@(.*/)") or "plugins/yiyan.koplugin/"
local Wallpaper = dofile(plugin_dir .. "wallpaper.lua")

local Yiyan = WidgetContainer:extend{ name = "yiyan" }

local SAVE_DIR = "/mnt/us/koreader/screenshots/yiyan/"
local STARTUP_DELAY = 8
local CHECK_INTERVAL = 5 * 60
local RATE_LIMIT_INTERVAL = 60 * 60
local HTTP_TIMEOUT = 20
local POLL_INTERVAL = 0.125
local UTC_OFFSET = 8 * 3600 -- 一言壁纸统一按北京时间归档
local DAY_SECONDS = 24 * 60 * 60

local SETTING_AUTO = "yiyan_auto_screensaver"
local SETTING_BACKUP = "yiyan_screensaver_backup"
local SETTING_ATTEMPT_HISTORY = "yiyan_attempt_history"
local SETTING_RATE_LIMIT = "yiyan_rate_limited_until"
local SETTING_SAVE_DIR = "yiyan_save_dir"
local SETTING_CATEGORIES = "yiyan_categories"
local SETTING_COVER_STYLE = "yiyan_cover_style"
local CACHE_OWNER = "yiyan.koplugin"
local CATEGORIES = {
    { "a", _("动画") }, { "b", _("漫画") }, { "c", _("游戏") },
    { "d", _("文学") }, { "e", _("原创") }, { "f", _("来自网络") },
    { "g", _("其他") }, { "h", _("影视") }, { "i", _("诗词") },
    { "j", _("网易云") }, { "k", _("哲学") }, { "l", _("抖机灵") },
}
local COVER_STYLES = {
    { Wallpaper.DEFAULT_STYLE, _("原版半露 CD（默认）") },
    { "calendar", _("白底日期卡") },
    { "dark", _("纯色深灰卡") },
}

local function selectedCoverStyle()
    return Wallpaper.coverStyle(G_reader_settings:readSetting(SETTING_COVER_STYLE))
end

local active_worker
local active_instance
local last_request
local staging_id = 0
local function workerNow() return time.to_s(time.boottime_or_monotonic()) end
local function todayDate(now)
    return os.date("!%Y%m%d", (now or os.time()) + UTC_OFFSET)
end

local function readFile(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local data = f:read("*a")
    f:close()
    return data
end

local function stageFile(path)
    local parent = path:match("^(.*)/[^/]+$")
    -- Reserve a directory on the same filesystem. mkdir is exclusive, so even
    -- preexisting staging names and files from other applications are untouched.
    local prefix = parent .. "/.yiyan-work-" .. os.time() .. "-"
    for _ = 1, 32 do
        staging_id = staging_id + 1
        local dir = prefix .. staging_id
        if lfs.mkdir(dir) then return dir .. "/data", dir end
        if not lfs.symlinkattributes(dir, "mode") then return nil end
    end
end

local function discardStage(path, dir)
    os.remove(path)
    lfs.rmdir(dir)
end

local function writeFile(path, data)
    local f = io.open(path, "wb")
    if not f then return false end
    local written = f:write(data)
    local closed = f:close()
    return written and closed
end

local function writeAtomic(path, data)
    local tmp, dir = stageFile(path)
    if not tmp then return false end
    local written = writeFile(tmp, data) and os.rename(tmp, path)
    discardStage(tmp, dir)
    return written
end

local function validPayload(payload)
    return type(payload) == "table" and type(payload.hitokoto) == "string"
        and payload.hitokoto:match("%S") ~= nil and #payload.hitokoto <= 8192
end

local function decodeCache(raw, date)
    if not raw then return nil end
    local ok, cached = pcall(json.decode, raw)
    if ok and type(cached) == "table" and cached.date == date
        and (cached.owner == nil or cached.owner == CACHE_OWNER)
        and validPayload(cached.payload) then
        return cached
    end
end

local function loadCache(dir, date)
    local path = dir .. date .. ".json"
    local mode = lfs.symlinkattributes(path, "mode")
    local raw = mode == "file" and readFile(path) or nil
    local cached = decodeCache(raw, date)
    return cached, mode ~= nil and cached == nil, raw
end

local function saveCache(dir, cached)
    local ok, raw = pcall(json.encode, cached)
    return ok and type(raw) == "string" and writeAtomic(dir .. cached.date .. ".json", raw)
end

local function parseAttempt(date, marker)
    local data = marker and marker:match("^" .. date .. "\n(.*)$")
    if not data then return end
    local attempt = {}
    if data:sub(1, 8) == "success\n" then
        attempt.success = true
        data = data:sub(9)
    end
    local retry_at, remaining = data:match("^(%d+)\n(.*)$")
    if retry_at then
        attempt.retry_at = tonumber(retry_at)
        data = remaining
    end
    if data == "" then return attempt end -- also accepts a date-only legacy marker
    attempt.stage = data:match("^(%.yiyan%-work%-%d+%-%d+)\n$")
    if attempt.stage then return attempt end
end

local function attemptData(date, success, retry_at, stage)
    local data = date .. "\n"
    if success then data = data .. "success\n" end
    if retry_at then data = data .. retry_at .. "\n" end
    if stage then data = data .. stage .. "\n" end
    return data
end

local function readAttempt(dir, date)
    local path = dir .. date .. ".attempt"
    local mode = lfs.symlinkattributes(path, "mode")
    return mode == "file" and parseAttempt(date, readFile(path)) or nil, mode
end

local function attemptHistory()
    local history = G_reader_settings:readSetting(SETTING_ATTEMPT_HISTORY)
    return type(history) == "table" and history or {}
end

local function saveAttemptHistory(date, status)
    local history = attemptHistory()
    if history[date] ~= status then
        history[date] = status
        G_reader_settings:saveSetting(SETTING_ATTEMPT_HISTORY, history)
        G_reader_settings:flush()
    end
end

local function wasSuccessful(status)
    return status == "success" or status == "cancelled_success"
end

local function writeSuccessMarker(dir, date, completed)
    local attempt, mode = readAttempt(dir, date)
    if attempt and attempt.success and (not completed or not attempt.stage) then return true end
    if not mode or attempt then
        return writeAtomic(dir .. date .. ".attempt", date .. "\nsuccess\n")
    end
end

local function rememberSuccess(dir, date, completed)
    if completed or attemptHistory()[date] ~= "cancelled_success" then
        saveAttemptHistory(date, "success")
    end
    return writeSuccessMarker(dir, date, completed)
end

local function readStagedResponse(path, date)
    if lfs.symlinkattributes(path, "mode") ~= "file" then return end
    local raw = readFile(path)
    local record = decodeCache(raw, date)
    if record and record.owner == CACHE_OWNER then return record, raw end
end

local function loadStagedCache(dir, date)
    local status = attemptHistory()[date]
    if status == "cancelled" or status == "cancelled_success" then return end
    local attempt = readAttempt(dir, date)
    local name = attempt and attempt.stage
    if not name then return end
    local work_dir = dir .. name
    if lfs.symlinkattributes(work_dir, "mode") ~= "directory" then return end
    for _, name in ipairs({ "/data", "/data.partial" }) do
        local path = work_dir .. name
        local record, raw = readStagedResponse(path, date)
        -- A failed local rename can leave a complete, usable response.
        if record then return { record = record, path = path, dir = work_dir, raw = raw } end
    end
end

local function sameSentence(a, b)
    return a.hitokoto == b.hitokoto and a.uuid == b.uuid
        and (a.from or "") == (b.from or "") and (a.from_who or "") == (b.from_who or "")
end

local function rememberDirectoryAttempts(dir)
    if lfs.attributes(dir, "mode") ~= "directory" then return true end
    local ok, entries, handle = pcall(lfs.dir, dir)
    if not ok then return false end
    local history = attemptHistory()
    for name in entries, handle do
        local date = name:match("^(%d%d%d%d%d%d%d%d)%.attempt$") or name:match("^(%d%d%d%d%d%d%d%d)%.json$")
        if date and not wasSuccessful(history[date]) then
            local attempt = readAttempt(dir, date)
            if (attempt and attempt.success) or loadCache(dir, date) or loadStagedCache(dir, date) then
                history[date] = "success"
            elseif history[date] ~= "cancelled" and attempt and attempt.retry_at and attempt.retry_at > os.time() then
                local previous = type(history[date]) == "number" and history[date] or 0
                history[date] = math.max(previous, attempt.retry_at)
            end
        end
    end
    G_reader_settings:saveSetting(SETTING_ATTEMPT_HISTORY, history)
    return true
end

local function encodeResponse(raw, date, replacement)
    local ok, payload = pcall(json.decode, raw)
    if not ok or not validPayload(payload) then return nil end
    return json.encode{ date = date, owner = CACHE_OWNER, replacement = replacement,
        image_digest = replacement and replacement.image_digest, payload = { hitokoto = payload.hitokoto,
        from = type(payload.from) == "string" and payload.from or "",
        from_who = type(payload.from_who) == "string" and payload.from_who or "",
        type = type(payload.type) == "string" and payload.type or nil,
        uuid = type(payload.uuid) == "string" and payload.uuid or nil } }
end

local function readCategories()
    local saved = G_reader_settings:readSetting(SETTING_CATEGORIES)
    local selected = {}
    if type(saved) == "table" then
        for _, category in ipairs(CATEGORIES) do
            local code = category[1]
            if saved[code] == true then selected[code] = true end
        end
    end
    return selected
end

local function requestURL()
    local selected = readCategories()
    local query = { "encode=json", "charset=utf-8" }
    for _, category in ipairs(CATEGORIES) do
        local code = category[1]
        if selected[code] then query[#query + 1] = "c=" .. code end
    end
    return "https://v1.hitokoto.cn/?" .. table.concat(query, "&")
end

local function networkIsConnected()
    if NetworkMgr and NetworkMgr.isConnected then
        local ok, connected = pcall(NetworkMgr.isConnected, NetworkMgr)
        if ok then return connected == true end
    end
    return false
end

local function connectionStatus(connected)
    if connected then return _("已连接本地网络") end
    if NetworkMgr and NetworkMgr.isWifiOn then
        local ok, on = pcall(NetworkMgr.isWifiOn, NetworkMgr)
        if ok and on == true then return _("已开启，尚未连通") end
        if ok and on == false then return _("已关闭") end
    end
    return _("未检测到连接")
end

local function retryWindow(marker, history_status, now)
    local rate_limit = G_reader_settings:readSetting(SETTING_RATE_LIMIT)
    local limited = type(rate_limit) == "number" and now < rate_limit
    local saved_retry = type(history_status) == "number" and history_status or 0
    local deadline = math.max(marker and marker.retry_at or 0, saved_retry, limited and rate_limit or 0)
    return deadline, limited
end

local function requestResultText(status)
    local messages = {
        success = _("成功收到句子"),
        network_error = _("网络请求失败"),
        timeout = _("请求超时"),
        invalid_response = _("接口未返回有效句子"),
        not_started = _("后台任务未能启动"),
        stale_date = _("临近北京时间零点，已推迟"),
        received_unstored = _("收到句子，但无法保存"),
        cancelled = _("请求已取消"),
        cancelled_success = _("请求已取消，收到的句子未保留"),
        http_429 = _("服务限流（HTTP 429）"),
    }
    local code = status:match("^http_(%d%d%d)$")
    return messages[status] or (code and _("接口返回 HTTP %s"):format(code)) or _("请求失败")
end

local function autoScreensaverEnabled()
    local enabled = G_reader_settings:readSetting(SETTING_AUTO)
    return enabled == nil or enabled == true
end

local function setAutoScreensaver(enabled)
    G_reader_settings:saveSetting(SETTING_AUTO, enabled)
    G_reader_settings:flush()
end

-- KOReader chooses a random image in this directory. Publish only the
-- current wallpaper here so older or user-supplied files cannot be selected.
local function screensaverDir(dir)
    return dir .. ".yiyan-screen/"
end

local function publishWallpaper(dir, path)
    local display_dir = screensaverDir(dir)
    local target = display_dir .. "wallpaper.png"
    local data = readFile(path)
    if not data then return false end
    if readFile(target) == data then return true end
    util.makePath(display_dir)
    return writeAtomic(target, data)
end

local function matchesArchivedImage(path, cached)
    if not cached or (not cached.owner and not cached.image_digest) then return true end
    local image = lfs.symlinkattributes(path, "mode") == "file" and readFile(path)
    return image and md5(image) == cached.image_digest
end

local function matchesRecordedDigest(digest, cached)
    local pending = cached.pending_image
    return digest and (digest == cached.image_digest
        or (type(pending) == "table" and digest == pending.digest))
end

local function knownBackup(backup)
    return type(backup) == "table" and (backup.version == 1 or backup.version == 2)
end

local function ownsScreensaverDir(backup, dir)
    local current_dir = G_reader_settings:readSetting("screensaver_dir")
    if backup.applied_dir then return current_dir == backup.applied_dir end
    -- Older backups did not record the directory the plugin applied.
    return current_dir == screensaverDir(dir) or current_dir == dir
end

local function matchesSnapshot(key, saved)
    if type(saved) ~= "table" then return false end
    local value = G_reader_settings:readSetting(key)
    if saved.is_set then return value == saved.value end
    return value == nil
end

local function configureScreensaver(dir, previous_dir)
    local display_dir = screensaverDir(dir)
    local settings = G_reader_settings
    local backup = settings:readSetting(SETTING_BACKUP)
    if knownBackup(backup) and settings:readSetting("screensaver_type") == "random_image"
        and ownsScreensaverDir(backup, previous_dir or dir) then
        if settings:readSetting("screensaver_dir") == display_dir and backup.applied_dir then
            if backup.version == 1 then
                backup.version = 2
                settings:saveSetting(SETTING_BACKUP, backup)
                settings:flush()
            end
            return true
        end
        if not Wallpaper.isValidPNG(display_dir .. "wallpaper.png") then return false end
        backup.applied_dir = display_dir
        backup.version = 2
        settings:saveSetting(SETTING_BACKUP, backup)
        settings:saveSetting("screensaver_dir", display_dir)
        settings:flush()
        return true
    end
    if knownBackup(backup) then
        -- Old builds saved the backup separately. Resume that unfinished apply
        -- only when all four settings still match the original snapshot.
        if backup.version ~= 1 or not backup.applied_dir
            or not matchesSnapshot("screensaver_type", backup.type)
            or not matchesSnapshot("screensaver_dir", backup.dir)
            or not matchesSnapshot("screensaver_stretch_images", backup.stretch)
            or not matchesSnapshot("screensaver_show_message", backup.show_message) then return false end
    end

    if not Wallpaper.isValidPNG(display_dir .. "wallpaper.png") then return false end
    local function snapshot(key)
        local value = settings:readSetting(key)
        return { is_set = value ~= nil, value = value }
    end
    backup = {
        version = 2,
        applied_dir = display_dir,
        type = snapshot("screensaver_type"),
        dir = snapshot("screensaver_dir"),
        stretch = snapshot("screensaver_stretch_images"),
        show_message = snapshot("screensaver_show_message"),
    }
    settings:saveSetting(SETTING_BACKUP, backup)
    settings:saveSetting("screensaver_type", "random_image")
    settings:saveSetting("screensaver_dir", display_dir)
    settings:saveSetting("screensaver_stretch_images", true)
    settings:saveSetting("screensaver_show_message", false)
    settings:flush()
    return true
end

local function restoreScreensaver(disable_auto)
    local settings = G_reader_settings
    local backup = settings:readSetting(SETTING_BACKUP)
    local restored = false
    if knownBackup(backup) then
        local function restore(key, saved)
            if type(saved) ~= "table" or matchesSnapshot(key, saved) then return end
            if not saved.is_set then
                settings:delSetting(key)
            else
                settings:saveSetting(key, saved.value)
            end
            restored = true
        end
        local owns_dir = ownsScreensaverDir(backup, SAVE_DIR)
        -- A manually selected random-image directory still needs this mode.
        if owns_dir and settings:readSetting("screensaver_type") == "random_image" then
            restore("screensaver_type", backup.type)
        end
        if owns_dir then restore("screensaver_dir", backup.dir) end
        if settings:readSetting("screensaver_stretch_images") == true then
            restore("screensaver_stretch_images", backup.stretch)
        end
        if settings:readSetting("screensaver_show_message") == false then
            restore("screensaver_show_message", backup.show_message)
        end
    end
    if disable_auto then settings:saveSetting(SETTING_AUTO, false) end
    if backup ~= nil then settings:delSetting(SETTING_BACKUP) end
    if backup ~= nil or disable_auto then settings:flush() end
    return restored
end

function Yiyan:init()
    if active_instance then active_instance:onCloseWidget() end
    active_instance = self
    self.ui.menu:registerToMainMenu(self)
    self._stopped = false
    self._busy = false
    local configured_dir = G_reader_settings:readSetting(SETTING_SAVE_DIR)
    if type(configured_dir) == "string" and configured_dir:match("^/mnt/us/") then
        SAVE_DIR = configured_dir:gsub("/*$", "") .. "/"
    end
    util.makePath(SAVE_DIR)
    if autoScreensaverEnabled() then configureScreensaver(SAVE_DIR) else restoreScreensaver(false) end
    self:_schedule(STARTUP_DELAY)
end

function Yiyan:_schedule(delay)
    if self._stopped then return end
    if self._check_callback then UIManager:unschedule(self._check_callback) end
    self._check_callback = function()
        self._check_callback = nil
        if self._stopped then return end
        self:refreshDaily(false)
        self:_schedule(CHECK_INTERVAL)
    end
    UIManager:scheduleIn(delay, self._check_callback)
end

function Yiyan:onResume()
    if not self._stopped then self:_schedule(2) end
end

function Yiyan:onNetworkConnected()
    if active_instance == self and not self._stopped then self:_schedule(2) end
end

function Yiyan:onCloseWidget()
    self._stopped = true
    if self._check_callback then UIManager:unschedule(self._check_callback); self._check_callback = nil end
    if active_instance == self then active_instance = nil end
end

function Yiyan:stopPlugin()
    self:onCloseWidget()
    if active_worker then active_worker.cancel() end
    restoreScreensaver(false)
end

function Yiyan:requestSentence(date, response_path, work_dir, replacement, callback)
    local url = requestURL()
    local partial = response_path .. ".partial"
    local started, pid, read_fd = pcall(ffiutil.runInSubProcess, function(_, child_write_fd)
        local result
        local open = http.open
        local date_boundary = false
        -- LuaSocket opens the connection before sending its HTTP request line.
        -- This hook is local to the child and keeps its default TLS/proxy setup.
        http.open = function(...)
            local connection = open(...)
            local now = os.time()
            local seconds_left = DAY_SECONDS - (now + UTC_OFFSET) % DAY_SECONDS
            if todayDate(now) ~= date or seconds_left <= HTTP_TIMEOUT then
                date_boundary = true
                connection:close()
                error("Beijing date boundary")
            end
            return connection
        end
        local ok = pcall(function()
            socketutil:set_timeout(HTTP_TIMEOUT, HTTP_TIMEOUT)
            local chunks, response_size = {}, 0
            -- The scheduler or a slow fork can carry the task across midnight.
            if todayDate() ~= date then
                result = "ERR\nstale_date"
                return
            end
            local request_ok, code = http.request{
                url = url,
                method = "GET",
                headers = { ["User-Agent"] = "KOReader-yiyan.koplugin/1.3.0" },
                redirect = false,
                sink = function(chunk, sink_err)
                    if sink_err then return nil, sink_err end
                    if chunk then
                        response_size = response_size + #chunk
                        if response_size > 65536 then return nil, "response_too_large" end
                        chunks[#chunks + 1] = chunk
                    end
                    return 1
                end,
            }
            if not request_ok then
                result = "ERR\nnetwork_error"
            elseif tonumber(code) ~= 200 then
                result = "ERR\nhttp_" .. tostring(code or "unknown")
            else
                local record = encodeResponse(table.concat(chunks), date, replacement)
                if type(record) ~= "string" then
                    result = "ERR\ninvalid_response"
                elseif writeFile(partial, record) and os.rename(partial, response_path) then
                    -- The marker points to this complete cache even after a
                    -- restart. Only the status needs to go through the pipe.
                    result = "ok"
                else
                    result = "ERR\nreceived_unstored"
                end
            end
        end)
        http.open = open
        if date_boundary then
            result = "ERR\nstale_date"
        elseif not ok then
            result = "ERR\nnetwork_error"
        end
        ffiutil.writeToFD(child_write_fd, result or "ERR\nnetwork_error", true)
    end, true)
    if not started or not pid then
        -- A normal failure return confirms no child started. An exception in a
        -- KOReader fork hook may occur after fork, so retain the attempt then.
        if started then discardStage(response_path, work_dir) end
        logger.warn("Yiyan: cannot start request worker", tostring(read_fd or pid))
        callback(false, started and "not_started" or "network_error")
        return
    end

    local worker = { deadline = workerNow() + HTTP_TIMEOUT }
    active_worker = worker
    UIManager:preventStandby()

    local function poll()
        if active_worker ~= worker then return end
        if not ffiutil.isSubProcessDone(pid) then
            worker.timed_out = worker.timed_out or workerNow() >= worker.deadline
            if worker.cancelled or worker.timed_out then
                -- A kill can race the child's initial process-group setup.
                -- Retry until waitpid confirms the old writer has exited.
                ffiutil.terminateSubProcess(pid)
            end
            UIManager:scheduleIn(POLL_INTERVAL, poll)
            return
        end

        active_worker = nil
        UIManager:allowStandby()
        local read_ok, response = pcall(ffiutil.readAllFromFD, read_fd)
        local err = read_ok and type(response) == "string" and response:match("^ERR\n([%w_]+)$")
        local staged = lfs.symlinkattributes(response_path, "mode") == "file"
            or readStagedResponse(partial, date) ~= nil
        if worker.cancelled then
            os.remove(partial)
            discardStage(response_path, work_dir)
            if staged or err == "received_unstored" then
                callback(false, "cancelled_success")
            elseif err == "http_429" then
                callback(false, err)
            end
            return
        end
        if staged then
            callback(true)
            return
        end
        os.remove(partial)
        discardStage(response_path, work_dir)
        -- A complete rate-limit status must survive a race with termination.
        if worker.timed_out and err ~= "http_429" and err ~= "received_unstored" and err ~= "stale_date" then
            callback(false, "timeout")
        else
            callback(false, err or "network_error")
        end
    end
    worker.cancel = function()
        if worker.cancelled then return end
        worker.cancelled = true
        callback(false, "cancelled")
        UIManager:unschedule(poll)
        poll()
    end
    UIManager:scheduleIn(POLL_INTERVAL, poll)
end

function Yiyan:renderImage(payload, date, target, style)
    local width, height = Device.screen:getWidth(), Device.screen:getHeight()
    return Wallpaper.render(self.path or plugin_dir:gsub("/$", ""), payload, date, target,
        math.min(width, height), math.max(width, height), style)
end

function Yiyan:_renderCache(dir, cached, image_path, show_message)
    local function notify(message)
        if show_message and not self._stopped then UIManager:show(InfoMessage:new{ text = message }) end
    end

    -- This field only authorizes promotion of a staged manual response.
    cached.replacement = nil
    local mode = lfs.symlinkattributes(image_path, "mode")
    local current = mode == "file" and readFile(image_path)
    local digest = current and md5(current)
    local style = selectedCoverStyle()
    local pending = cached.pending_image
    local recovered = digest and type(pending) == "table" and digest == pending.digest
        and type(pending.layout_version) == "number" and Wallpaper.isValidPNG(image_path)
    if recovered and not Wallpaper.hasCurrentLayout(pending, style) then
        -- The interrupted render owns this PNG, even when another style is now selected.
        cached.image_digest = pending.digest
        recovered = false
    end
    if not recovered then
        local legacy = not cached.owner and (cached.layout_version == 1 or cached.layout_version == 2)
        if mode and (not digest or (digest ~= cached.image_digest and not legacy)) then
            notify(_("同名图片无法确认归属，请移走后再重新生成"))
            return false
        end
        local tmp, work_dir = stageFile(image_path)
        if not tmp then
            notify(_("无法创建图片临时文件，请检查存储空间和目录权限"))
            return false
        end
        local rendered = self:renderImage(cached.payload, cached.date, tmp, style) and readFile(tmp)
        if not rendered then
            discardStage(tmp, work_dir)
            notify(_("图片生成失败，句子已缓存，下次检查时会重试"))
            return false
        end

        -- Record both digests before replacing the archive. After an interrupted
        -- update, either the previous image or the staged one can be identified.
        pending = { digest = md5(rendered), layout_version = Wallpaper.VERSION, cover_style = style }
        cached.pending_image = pending
        local replaced = saveCache(dir, cached) and os.rename(tmp, image_path)
        discardStage(tmp, work_dir)
        if not replaced then
            notify(_("图片更新未完成，下次检查时会重试"))
            return false
        end
    end

    cached.layout_version = pending.layout_version
    cached.cover_style = pending.cover_style or Wallpaper.DEFAULT_STYLE
    cached.owner = CACHE_OWNER
    cached.image_digest = pending.digest
    cached.pending_image = nil
    if not saveCache(dir, cached) then
        notify(_("图片已生成，但缓存保存失败，可稍后离线恢复"))
        return false
    end

    local published = publishWallpaper(dir, image_path)
    if published then self:cleanupOldImages(dir, cached.date) end
    if published and not self._stopped and autoScreensaverEnabled() then
        configureScreensaver(dir)
    end
    notify(published and _("今日一言壁纸已生成") or _("图片已生成，但未能更新屏保文件"))
    return published
end

function Yiyan:cleanupOldImages(dir, keep_date)
    if lfs.attributes(dir, "mode") ~= "directory" then return end
    local ok, entries, handle = pcall(lfs.dir, dir)
    if not ok then
        logger.warn("Yiyan: could not list wallpaper directory", entries)
        return
    end
    for name in entries, handle do
        local date = name:match("^(%d%d%d%d%d%d%d%d)%.json$")
        if date and date < keep_date then
            local cached = loadCache(dir, date)
            if cached and cached.owner == CACHE_OWNER then
                local path = dir .. date .. ".png"
                local mode = lfs.symlinkattributes(path, "mode")
                local image = mode == "file" and readFile(path)
                local digest = image and md5(image)
                -- Keep the ownership record alongside a replacement. Without
                -- it, a clock rollback could mistake the PNG for a legacy one.
                if not mode or (matchesRecordedDigest(digest, cached) and os.remove(path)) then
                    -- Upgrade legacy attempts before deleting their only proof
                    -- of a successful request.
                    if rememberSuccess(dir, date) then os.remove(dir .. name) end
                end
            end
        end
    end
end

function Yiyan:refreshDaily(show_message, fetch_new, local_only)
    if self._stopped then return end
    local function notify(message)
        if show_message and not self._stopped then UIManager:show(InfoMessage:new{ text = message }) end
    end
    if self._busy or active_worker then notify(_("一言任务正在进行中")); return end
    local dir = SAVE_DIR
    util.makePath(dir)
    local date = todayDate()
    local image_path = dir .. date .. ".png"
    local cache_path = dir .. date .. ".json"
    local attempt_path = dir .. date .. ".attempt"

    local function finishResponse()
        local staged = loadStagedCache(dir, date)
        if not staged then return false end
        local existing, conflict, raw = loadCache(dir, date)
        local replacement = staged.record.replacement
        local replacing = type(replacement) == "table" and type(replacement.cache_digest) == "string"
        -- A staged ordinary fetch cannot replace an already published cache.
        if existing and not replacing then return false end
        local image_mode = lfs.symlinkattributes(image_path, "mode")
        local image = replacing and image_mode == "file" and readFile(image_path)
        local cache_matches = not existing or md5(raw) == replacement.cache_digest
        local image_matches = not image_mode or (image and md5(image) == staged.record.image_digest)
        saveAttemptHistory(date, "success")
        if conflict or not cache_matches or not image_matches then
            notify(_("发现同名文件，已保留原文件和今日句子，请移走冲突文件后重试"))
        elseif not os.rename(staged.path, cache_path) then
            notify(_("新句子已保留，请检查存储空间后重试"))
        else
            discardStage(staged.path, staged.dir)
            rememberSuccess(dir, date, true)
            if todayDate() == date then self:_renderCache(dir, staged.record, image_path, show_message) end
        end
        return true
    end

    local cached, cache_conflict, cached_raw = loadCache(dir, date)
    if finishResponse() then return end
    if cache_conflict then
        notify(_("同名 JSON 无法识别为一言缓存，请移走后再生成"))
        return
    end
    if local_only and not cached then
        notify(_("封面类型已保存，取得句子后将使用新样式"))
        return
    end
    if cached then rememberSuccess(dir, date) end
    if not fetch_new and Wallpaper.isValidPNG(image_path)
        and (not cached or (not cached.pending_image and Wallpaper.hasCurrentLayout(cached, selectedCoverStyle()))) then
        if not matchesArchivedImage(image_path, cached) then
            notify(_("同名图片无法确认归属，请移走后再重新生成"))
            return
        end
        if not cached then rememberSuccess(dir, date) end
        local published = publishWallpaper(dir, image_path)
        if published and autoScreensaverEnabled() then configureScreensaver(dir) end
        notify(published and _("今日一言壁纸已存在") or _("图片已生成，但未能更新屏保文件"))
        return
    end
    -- Rebuilding from a cached sentence never opens a network connection, even
    -- if today's image is missing or the template changed since it was saved.
    if cached and not fetch_new then self:_renderCache(dir, cached, image_path, show_message); return end
    local image_mode = lfs.symlinkattributes(image_path, "mode")
    local image = cached and fetch_new and image_mode == "file" and readFile(image_path)
    local image_digest = image and md5(image)
    local legacy_cache = cached and not cached.owner and (cached.layout_version == 1 or cached.layout_version == 2)
    if image_mode and not (image_digest and (matchesRecordedDigest(image_digest, cached) or legacy_cache)) then
        notify(_("同名图片无法确认归属，请移走后再生成"))
        return
    end
    local replacement = fetch_new and cached and { cache_digest = md5(cached_raw), image_digest = image_digest }
        or nil
    local marker, marker_mode = readAttempt(dir, date)
    local history = attemptHistory()
    local prior_success = cached or (marker and marker.success) or wasSuccessful(history[date])
    if prior_success and not fetch_new then
        notify(_("今日已成功获取一言，但本地句子缓存不可用"))
        return
    end
    if marker_mode and not marker then
        notify(_("同名请求记录无法识别，请移走后再生成"))
        return
    end
    if not networkIsConnected() then
        notify(_("网络未连接，请联网后重试"))
        return
    end
    local now = os.time()
    local deadline, limited = retryWindow(marker, history[date], now)
    if limited then
        notify(_("一言服务暂时限流，稍后会自动重试"))
        return
    end
    if now < deadline then
        notify(prior_success and _("上次请求未完成，请稍后手动重试")
            or _("上次请求未完成，稍后会自动重试"))
        return
    end

    -- Reserve a private response path and persist the attempt before forking.
    local response_path, work_dir = stageFile(attempt_path)
    if not response_path then
        notify(_("无法创建句子临时文件，请检查存储空间和目录权限"))
        return
    end
    local retry_at = now + CHECK_INTERVAL
    local attempt = attemptData(date, prior_success, retry_at, work_dir:match("[^/]+$"))
    if not writeAtomic(attempt_path, attempt) then
        discardStage(response_path, work_dir)
        notify(_("无法保存请求记录，请检查存储空间和目录权限"))
        return
    end
    saveAttemptHistory(date, prior_success and "success" or nil)
    local function ownsAttempt()
        return lfs.symlinkattributes(attempt_path, "mode") == "file" and readFile(attempt_path) == attempt
    end
    self._busy = true
    self:requestSentence(date, response_path, work_dir, replacement, function(received, err)
        self._busy = false
        last_request = { date = date, status = received and "success" or err or "network_error" }
        if err == "cancelled" then
            -- Revoke the response pointer before KOReader can exit and drop
            -- the poll callback. Preserve a known success without publishing it.
            if ownsAttempt() then
                local staged = loadStagedCache(dir, date)
                local marker_data = attemptData(date, staged or prior_success, not staged and retry_at)
                local revoked = writeAtomic(attempt_path, marker_data)
                if staged then
                    saveAttemptHistory(date, revoked and "success" or "cancelled_success")
                elseif not revoked then
                    saveAttemptHistory(date, prior_success and "cancelled_success" or "cancelled")
                end
            end
        elseif err == "cancelled_success" then
            saveAttemptHistory(date, writeSuccessMarker(dir, date, true) and "success" or "cancelled_success")
        elseif err == "not_started" or err == "stale_date" then
            -- Clear only the reservation we wrote, and only when the worker
            -- confirmed that no request could have been made.
            if ownsAttempt() then
                if prior_success then
                    writeSuccessMarker(dir, date, true)
                else
                    os.remove(attempt_path)
                end
            end
            notify(err == "stale_date" and _("临近北京时间零点，将在下次检查时更新")
                or _("后台任务未能启动，可稍后重试"))
        elseif err == "received_unstored" then
            rememberSuccess(dir, date, true)
            notify(_("已收到今日句子，但无法保存，请检查存储空间和目录权限"))
        elseif not received then
            logger.warn("Yiyan: daily request failed", tostring(err))
            if err == "http_429" then
                local limited_until = os.time() + RATE_LIMIT_INTERVAL
                -- Service throttling also applies after the Beijing date changes.
                G_reader_settings:saveSetting(SETTING_RATE_LIMIT, limited_until)
                G_reader_settings:flush()
                if ownsAttempt() then
                    writeAtomic(attempt_path, attemptData(date, prior_success, limited_until))
                end
                notify(_("一言服务暂时限流，一小时后再试"))
            else
                notify(prior_success and _("未能取得新句子，已保留当前壁纸")
                    or _("今日一言请求失败，稍后会自动重试"))
            end
        elseif not finishResponse() then
            saveAttemptHistory(date, "success")
            notify(_("已收到今日句子，但暂时无法读取缓存"))
        end
    end)
end

function Yiyan:networkDiagnostics()
    local now = os.time()
    local date = todayDate(now)
    local dir = SAVE_DIR
    local connected = networkIsConnected()
    local marker, marker_mode = readAttempt(dir, date)
    local history_status = attemptHistory()[date]
    local cached, cache_conflict = loadCache(dir, date)
    local staged = loadStagedCache(dir, date)
    local replacing = staged and staged.record.replacement
    local image_path = dir .. date .. ".png"
    local image_exists = lfs.symlinkattributes(image_path, "mode") ~= nil
    local valid_image = image_exists and Wallpaper.isValidPNG(image_path)
    local usable_image = valid_image and not cache_conflict and matchesArchivedImage(image_path, cached)
    local legacy_image = usable_image and not cached and not staged

    local sentence = _("尚未获取")
    if replacing then
        sentence = _("新句子已收到，等待更新")
    elseif cached then
        sentence = _("已缓存")
    elseif staged then
        sentence = (cache_conflict or image_exists) and _("已收到，等待处理同名文件")
            or _("已收到，等待保存到缓存")
    elseif (marker and marker.success) or wasSuccessful(history_status) then
        sentence = _("已获取，缓存不可用")
    elseif legacy_image then
        sentence = _("旧版壁纸无句子缓存")
    end

    local wallpaper = _("尚未生成")
    if cached and cached.pending_image then
        wallpaper = _("等待完成更新")
    elseif usable_image then
        wallpaper = (cached and not Wallpaper.hasCurrentLayout(cached, selectedCoverStyle())) and _("等待重新生成")
            or _("已就绪")
    elseif image_exists then
        wallpaper = _("文件无法使用")
    end

    local deadline, limited = retryWindow(marker, history_status, now)
    local fetched = cached or staged or (marker and marker.success) or wasSuccessful(history_status) or legacy_image
    local request
    if self._busy or active_worker then
        request = _("后台请求进行中")
    elseif (marker_mode and not marker) or (not fetched and (cache_conflict or image_exists)) then
        request = _("同名文件冲突")
    elseif now < deadline then
        local wait = _("%d 分钟后可重试"):format(math.ceil((deadline - now) / 60))
        local reason = limited and _("服务限流") or (fetched and _("手动刷新") or _("等待重试"))
        request = reason .. "（" .. wait .. "）"
    elseif fetched then
        request = _("今日无需自动请求")
    elseif not connected then
        request = _("等待联网")
    else
        request = _("等待下次检查")
    end

    local lines = {
        _("Wi-Fi") .. "  " .. connectionStatus(connected),
        _("今日句子") .. "  " .. sentence,
        _("今日壁纸") .. "  " .. wallpaper,
        _("请求状态") .. "  " .. request,
    }
    if last_request and last_request.date == date then
        lines[#lines + 1] = _("最近请求") .. "  " .. requestResultText(last_request.status)
    end
    return table.concat(lines, "\n")
end

function Yiyan:chooseOutputDirectory()
    if self._busy or active_worker then
        UIManager:show(InfoMessage:new{ text = _("请等待当前任务完成后再修改目录") })
        return
    end
    local dialog
    dialog = InputDialog:new{
        title = _("屏保图片目录"),
        input = SAVE_DIR,
        input_hint = _("请输入 Kindle 上的绝对目录路径"),
        buttons = {{
            { text = _("取消"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("保存"), callback = function()
                if self._busy or active_worker then
                    UIManager:show(InfoMessage:new{ text = _("请等待当前任务完成后再修改目录") })
                    return
                end
                local path = dialog:getInputText()
                if not path or not path:match("^/mnt/us/") then
                    UIManager:show(InfoMessage:new{ text = _("请输入 /mnt/us/ 下的目录") })
                    return
                end
                local dir = path:gsub("/+", "/"):gsub("/$", "") .. "/"
                if dir:find("/../", 1, true) or dir:find("/./", 1, true) then
                    UIManager:show(InfoMessage:new{ text = _("目录路径不能包含 . 或 ..") })
                    return
                end
                local old_dir = SAVE_DIR
                if dir ~= old_dir then
                    if not util.makePath(dir) then
                        UIManager:show(InfoMessage:new{ text = _("无法创建目录，请检查存储空间和目录权限") })
                        return
                    end
                    local date = todayDate()
                    if select(2, loadCache(dir, date)) then
                        UIManager:show(InfoMessage:new{ text = _("新目录的同名 JSON 无法识别为一言缓存") })
                        return
                    end
                    local staged = loadStagedCache(old_dir, date)
                    local destination_staged = loadStagedCache(dir, date)
                    local source_cache, source_conflict = loadCache(old_dir, date)
                    if staged and staged.record.replacement and (source_cache or source_conflict) then
                        UIManager:show(InfoMessage:new{ text = _("新句子正在等待保存，请完成壁纸更新后再切换目录") })
                        return
                    end
                    local staged_only = staged and not source_cache
                    local source_record = source_cache or (staged and staged.record)
                    local stage_matches = destination_staged and source_record
                        and sameSentence(destination_staged.record.payload, source_record.payload)
                    local copies = {}
                    for _index, suffix in ipairs({ ".png", ".json", ".attempt" }) do
                        local name = date .. suffix
                        local data
                        if staged_only then
                            -- Save the staged sentence directly as the new cache.
                            -- The source reservation points to a directory left behind.
                            if suffix == ".json" then
                                data = staged.raw
                            elseif suffix == ".attempt" then
                                data = date .. "\nsuccess\n"
                            end
                        elseif suffix == ".attempt" or not source_conflict then
                            local source_path = old_dir .. name
                            if lfs.symlinkattributes(source_path, "mode") == "file" then
                                data = readFile(source_path)
                            end
                            if suffix == ".attempt" then
                                local attempt = parseAttempt(date, data)
                                -- A staging directory belongs to the source.
                                if not attempt then
                                    data = nil
                                elseif attempt.stage then
                                    data = attemptData(date, attempt.success, attempt.retry_at)
                                end
                            elseif suffix == ".png" and data then
                                local recorded = source_cache and (source_cache.owner or source_cache.image_digest)
                                if (recorded and not matchesRecordedDigest(md5(data), source_cache))
                                    or not Wallpaper.isValidPNG(source_path) then data = nil end
                            end
                        end
                        local target = dir .. name
                        local mode = lfs.symlinkattributes(target, "mode")
                        local existing = mode == "file" and readFile(target) or nil
                        local same_attempt
                        if suffix == ".attempt" then
                            local source_attempt = parseAttempt(date, data)
                            local target_attempt = parseAttempt(date, existing)
                            if source_attempt and target_attempt then
                                local required = source_attempt.retry_at or 0
                                same_attempt = required <= math.max(os.time(), target_attempt.retry_at or 0)
                            end
                        end
                        if data and ((mode and data ~= existing and not same_attempt)
                            or (destination_staged and suffix ~= ".attempt" and not stage_matches)) then
                            UIManager:show(InfoMessage:new{ text = _("新目录已有不同的今日壁纸数据，请选择其他目录") })
                            return
                        end
                        if data and not mode then
                            copies[#copies + 1] = { path = target, data = data }
                        end
                    end
                    if not rememberDirectoryAttempts(old_dir) then
                        UIManager:show(InfoMessage:new{ text = _("无法读取原目录的请求记录，请检查目录后重试") })
                        return
                    end
                    for i, copy in ipairs(copies) do
                        if not writeAtomic(copy.path, copy.data) then
                            for j = 1, i - 1 do os.remove(copies[j].path) end
                            UIManager:show(InfoMessage:new{ text = _("无法复制今日壁纸数据，请检查目录权限和存储空间") })
                            return
                        end
                    end
                    local image_path = dir .. date .. ".png"
                    local cached, cache_conflict = loadCache(dir, date)
                    if cache_conflict or not Wallpaper.isValidPNG(image_path)
                        or (cached and cached.pending_image)
                        or not matchesArchivedImage(image_path, cached) then
                        image_path = screensaverDir(old_dir) .. "wallpaper.png"
                    end
                    if Wallpaper.isValidPNG(image_path) and not publishWallpaper(dir, image_path) then
                        for _, copy in ipairs(copies) do os.remove(copy.path) end
                        UIManager:show(InfoMessage:new{ text = _("无法更新新目录的屏保文件") })
                        return
                    end
                    SAVE_DIR = dir
                    G_reader_settings:saveSetting(SETTING_SAVE_DIR, dir)
                    G_reader_settings:flush()
                    if autoScreensaverEnabled() then configureScreensaver(dir, old_dir) end
                end
                UIManager:close(dialog)
            end },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function Yiyan:addToMainMenu(menu_items)
    local categories = {{
        text = _("默认随机"),
        checked_func = function() return next(readCategories()) == nil end,
        callback = function()
            G_reader_settings:delSetting(SETTING_CATEGORIES)
            G_reader_settings:flush()
        end,
    }}
    for _, category in ipairs(CATEGORIES) do
        local code, label = category[1], category[2]
        categories[#categories + 1] = {
            text = label,
            checked_func = function() return readCategories()[code] == true end,
            callback = function()
                local selected = readCategories()
                selected[code] = not selected[code] or nil
                G_reader_settings:saveSetting(SETTING_CATEGORIES, selected)
                G_reader_settings:flush()
            end,
        }
    end
    local covers = {}
    for _, cover in ipairs(COVER_STYLES) do
        local style, label = cover[1], cover[2]
        covers[#covers + 1] = {
            text = label,
            checked_func = function() return selectedCoverStyle() == style end,
            callback = function()
                if G_reader_settings:readSetting(SETTING_COVER_STYLE) ~= style then
                    G_reader_settings:saveSetting(SETTING_COVER_STYLE, style)
                    G_reader_settings:flush()
                end
                self:refreshDaily(true, false, true)
            end,
        }
    end
    menu_items.yiyan = {
        text = _("一言壁纸"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("自动配置 KOReader 屏保"),
                checked_func = autoScreensaverEnabled,
                callback = function()
                    if autoScreensaverEnabled() then
                        local restored = restoreScreensaver(true)
                        local message = restored and _("已关闭自动配置，已恢复插件修改的屏保设置")
                            or _("已关闭自动配置")
                        UIManager:show(InfoMessage:new{ text = message })
                    else
                        setAutoScreensaver(true)
                        local ready = configureScreensaver(SAVE_DIR)
                        local message = ready and _("已启用自动配置 KOReader 屏保")
                            or _("已开启自动配置，壁纸就绪后将自动切换屏保")
                        UIManager:show(InfoMessage:new{ text = message })
                    end
                end,
            },
            { text = _("句子类型（下次请求生效）"), sub_item_table = categories },
            { text = _("封面类型"), sub_item_table = covers },
            { text = _("重新生成今日壁纸"), callback = function() self:refreshDaily(true, true) end },
            { text = _("网络诊断"), callback = function()
                UIManager:show(InfoMessage:new{ text = self:networkDiagnostics() })
            end },
            { text = _("设置屏保图片目录"), callback = function() self:chooseOutputDirectory() end },
        },
    }
end

return Yiyan
