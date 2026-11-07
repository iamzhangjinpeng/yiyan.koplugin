-- Online release updater for yiyan.koplugin.

local Updater = {
    VERSION = "1.4.1",
    RELEASE_API = "https://api.github.com/repos/iamzhangjinpeng/yiyan.koplugin/releases/latest",
    RELEASE_BASE = "https://github.com/iamzhangjinpeng/yiyan.koplugin/releases/download/",
    MAX_PACKAGE_SIZE = 2 * 1024 * 1024,
}

Updater.FILES = {
    "main.lua",
    "wallpaper.lua",
    "updater.lua",
    "_meta.lua",
    "assets/lyrics-background.png",
    "assets/date-card-background.png",
    "assets/dark-card-background.png",
}

-- Install dependencies before main.lua, which loads updater.lua on startup.
Updater.INSTALL_ORDER = {
    "assets/lyrics-background.png",
    "assets/date-card-background.png",
    "assets/dark-card-background.png",
    "wallpaper.lua",
    "updater.lua",
    "main.lua",
    "_meta.lua",
}

local function parseVersion(version)
    if type(version) ~= "string" or #version > 32 then return end
    version = version:gsub("^v", "")
    local major, minor, patch = version:match("^(%d+)%.(%d+)%.(%d+)$")
    major, minor, patch = tonumber(major), tonumber(minor), tonumber(patch)
    if not major or not minor or not patch or major > 1000000 or minor > 1000000 or patch > 1000000 then
        return
    end
    return { major, minor, patch }
end

function Updater.isNewer(candidate, installed)
    local left, right = parseVersion(candidate), parseVersion(installed)
    if not left or not right then return false end
    for i = 1, 3 do
        if left[i] ~= right[i] then return left[i] > right[i] end
    end
    return false
end

function Updater.releaseToInstall(release, installed_version)
    if type(release) ~= "table" or release.draft or release.prerelease then
        return nil, "invalid_release"
    end
    local tag = release.tag_name
    local version = type(tag) == "string" and tag:match("^v(%d+%.%d+%.%d+)$")
    if not version or not parseVersion(version) then return nil, "invalid_version" end
    if not Updater.isNewer(version, installed_version) then return nil, "up_to_date" end

    local expected_name = "yiyan.koplugin-v" .. version .. "-install.zip"
    local expected_url = Updater.RELEASE_BASE .. tag .. "/" .. expected_name
    local assets = release.assets
    if type(assets) ~= "table" or type(assets[1]) ~= "table" then return nil, "missing_asset" end
    for _, asset in ipairs(assets) do
        if type(asset) == "table" and asset.name == expected_name
            and asset.browser_download_url == expected_url then
            local size = tonumber(asset.size)
            if not size or size <= 0 or size > Updater.MAX_PACKAGE_SIZE then
                return nil, "invalid_asset_size"
            end
            return { version = version, tag = tag, name = expected_name,
                url = expected_url, size = size }
        end
    end
    return nil, "missing_asset"
end

local function filePath(root, relative)
    return root .. "/" .. relative
end

local function readFile(path)
    local file = io.open(path, "rb")
    if not file then return nil end
    local data = file:read("*a")
    file:close()
    return data
end

local function cleanupArchive(archive)
    pcall(function() archive:close() end)
end

function Updater.extractPackage(zip_path, stage_dir, expected_version, validate_png)
    local lfs = require("libs/libkoreader-lfs")
    local util = require("util")
    local ok, Archiver = pcall(require, "ffi/archiver")
    if not ok or not Archiver or not Archiver.Reader then return false, "archive_reader_unavailable" end

    local reader_ok, archive = pcall(function() return Archiver.Reader:new() end)
    if not reader_ok or not archive then return false, "archive_reader_unavailable" end
    local opened_ok, opened = pcall(function() return archive:open(zip_path) end)
    if not opened_ok or not opened then
        local err = archive.err or "invalid_archive"
        cleanupArchive(archive)
        return false, err
    end

    local expected = {}
    for _, relative in ipairs(Updater.FILES) do
        expected["yiyan.koplugin/" .. relative] = relative
    end
    local seen, total_size, extract_error = {}, 0, nil
    local iterate_ok = pcall(function()
        for entry in archive:iterate() do
            local path = entry and entry.path
            if path == "yiyan.koplugin" or path == "yiyan.koplugin/"
                or path == "yiyan.koplugin/assets" or path == "yiyan.koplugin/assets/" then
                if entry.mode == "file" then extract_error = "unexpected_archive_entry"; break end
            else
                local relative = expected[path]
                local size = tonumber(entry.size)
                if not relative or entry.mode ~= "file" or seen[relative]
                    or not size or size <= 0 or size > Updater.MAX_PACKAGE_SIZE then
                    extract_error = "unexpected_archive_entry"
                    break
                end
                total_size = total_size + size
                if total_size > Updater.MAX_PACKAGE_SIZE then
                    extract_error = "package_too_large"
                    break
                end
                local destination = filePath(stage_dir, "yiyan.koplugin/" .. relative)
                local parent = destination:match("^(.*)/[^/]+$")
                util.makePath(parent)
                local extracted_ok, extracted = pcall(archive.extractToPath, archive, path, destination)
                if lfs.symlinkattributes(parent, "mode") ~= "directory" or not extracted_ok or not extracted then
                    extract_error = archive.err or "extract_failed"
                    break
                end
                seen[relative] = true
            end
        end
    end)
    cleanupArchive(archive)
    if not iterate_ok then return false, "invalid_archive" end
    if extract_error then return false, extract_error end
    for _, relative in ipairs(Updater.INSTALL_ORDER) do
        if not seen[relative] then return false, "missing_package_file" end
    end

    local staged_root = filePath(stage_dir, "yiyan.koplugin")
    local meta = readFile(filePath(staged_root, "_meta.lua"))
    local meta_version = meta and meta:match('[\n%s]version%s*=%s*"([^"]+)"')
    if meta_version ~= expected_version then return false, "version_mismatch" end

    for _, relative in ipairs({ "main.lua", "wallpaper.lua", "updater.lua" }) do
        local ok_load, chunk, err = pcall(loadfile, filePath(staged_root, relative))
        if not ok_load or not chunk then return false, err or "invalid_lua_file" end
    end
    if type(validate_png) ~= "function" then return false, "png_validator_unavailable" end
    for _, relative in ipairs({ "assets/lyrics-background.png", "assets/date-card-background.png",
        "assets/dark-card-background.png" }) do
        local ok_png, valid = pcall(validate_png, filePath(staged_root, relative))
        if not ok_png or not valid then return false, "invalid_wallpaper_asset" end
    end
    return true
end

function Updater.install(plugin_dir, stage_dir, copy_file)
    local lfs = require("libs/libkoreader-lfs")
    local util = require("util")
    if type(copy_file) ~= "function" then return false, "copy_unavailable" end

    local staged_root = filePath(stage_dir, "yiyan.koplugin")
    local backup_root = filePath(stage_dir, "backup")
    util.makePath(backup_root .. "/assets")
    if lfs.symlinkattributes(backup_root .. "/assets", "mode") ~= "directory" then
        return false, "cannot_create_backup"
    end

    local originals = {}
    for _, relative in ipairs(Updater.INSTALL_ORDER) do
        local destination = filePath(plugin_dir, relative)
        local source = filePath(staged_root, relative)
        local mode = lfs.symlinkattributes(destination, "mode")
        if (mode and mode ~= "file") or lfs.symlinkattributes(source, "mode") ~= "file" then
            return false, "unsafe_install_path"
        end
        originals[relative] = mode == "file"
        if originals[relative] then
            local backup = filePath(backup_root, relative)
            local parent = backup:match("^(.*)/[^/]+$")
            util.makePath(parent)
            local called, result = pcall(copy_file, destination, backup)
            if not called or (result ~= nil and result ~= true)
                or lfs.symlinkattributes(backup, "mode") ~= "file"
                or readFile(backup) ~= readFile(destination) then
                return false, "cannot_backup_install"
            end
        end
    end

    local touched = {}
    local install_error
    for _, relative in ipairs(Updater.INSTALL_ORDER) do
        local destination = filePath(plugin_dir, relative)
        local source = filePath(staged_root, relative)
        if originals[relative] and not os.remove(destination) then
            install_error = "cannot_replace_install"
            break
        end
        touched[relative] = true
        if not os.rename(source, destination) then
            install_error = "cannot_replace_install"
            break
        end
    end

    if install_error then
        local rollback_failed = false
        for i = #Updater.INSTALL_ORDER, 1, -1 do
            local relative = Updater.INSTALL_ORDER[i]
            if touched[relative] then
                local destination = filePath(plugin_dir, relative)
                os.remove(destination)
                if originals[relative] then
                    local backup = filePath(backup_root, relative)
                    local called, result = pcall(copy_file, backup, destination)
                    if not called or (result ~= nil and result ~= true)
                        or readFile(destination) ~= readFile(backup) then
                        rollback_failed = true
                    end
                end
            end
        end
        return false, rollback_failed and "rollback_incomplete" or install_error
    end
    return true
end

function Updater.cleanup(stage_dir)
    local lfs = require("libs/libkoreader-lfs")
    os.remove(filePath(stage_dir, "update.zip"))
    for _, relative in ipairs(Updater.FILES) do
        os.remove(filePath(stage_dir, "yiyan.koplugin/" .. relative))
        os.remove(filePath(stage_dir, "backup/" .. relative))
    end
    pcall(lfs.rmdir, filePath(stage_dir, "yiyan.koplugin/assets"))
    pcall(lfs.rmdir, filePath(stage_dir, "yiyan.koplugin"))
    pcall(lfs.rmdir, filePath(stage_dir, "backup/assets"))
    pcall(lfs.rmdir, filePath(stage_dir, "backup"))
    pcall(lfs.rmdir, stage_dir)
end

return Updater
