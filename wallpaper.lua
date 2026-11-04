-- White lyric-poster layout. All measurements are physical pixels, scaled from
-- the bundled 758x1024 template; text never overlaps the corner illustration.
local Blitbuffer = require("ffi/blitbuffer")
local ffi = require("ffi")
local Png = require("ffi/png")
local RenderImage = require("ui/renderimage")
local TextBoxWidget = require("ui/widget/textboxwidget")
local Font = require("ui/font")
local logger = require("logger")

local Wallpaper = { VERSION = 3 }

function Wallpaper.isValidPNG(path)
    local file = io.open(path, "rb")
    if not file then return false end
    local head = file:read(8)
    local size = file:seek("end")
    local tail
    if size and size >= 32 and file:seek("end", -12) then
        tail = file:read(12)
    end
    file:close()
    if head ~= "\137PNG\r\n\26\n" or tail ~= "\0\0\0\0IEND\174\66\96\130" then
        return false
    end
    local called, decoded, image = pcall(Png.decodeFromFile, path, 1)
    if not called or not decoded then return false end
    ffi.C.free(image.data)
    return true
end

local function round(value) return math.floor(value + 0.5) end

function Wallpaper.layout(width, height)
    local scale = math.min(width / 758, height / 1024)
    local margin = round(width * 0.112)
    local footer_y = round(height * 0.90)
    return {
        body = { x = margin, y = round(height * 0.155), width = width - 2 * margin,
            height = round(height * 0.45), font_size = math.max(20, round(34 * scale)),
            min_size = math.max(14, round(18 * scale)) },
        footer = { y = footer_y, height = height - footer_y },
        source = { x = margin, y = footer_y + round(height * 0.016),
            width = width - 2 * margin, height = round(height * 0.046),
            font_size = math.max(14, round(17 * scale)), min_size = math.max(12, round(13 * scale)) },
        date = { x = margin, y = footer_y + round((height - footer_y) * 0.70),
            width = width - 2 * margin, height = round(height * 0.025),
            font_size = math.max(11, round(12 * scale)), min_size = math.max(10, round(10 * scale)) },
    }
end

local function field(value)
    return type(value) == "string" and value:gsub("[%z\1-\31\127]", " ") or ""
end

function Wallpaper.source(payload)
    local author, source = field(payload.from_who), field(payload.from)
    if author ~= "" and source ~= "" then return author .. " · " .. source end
    if author ~= "" then return author end
    if source ~= "" then return source end
    return "每日一句"
end

function Wallpaper.body(payload)
    local text = payload.hitokoto:gsub("\r\n", "\n"):gsub("\r", "\n")
    -- Keep the original punctuation while making Chinese phrases read like
    -- lyric lines. The widget still wraps phrases that exceed the safe width.
    for _, mark in ipairs({ "，", "。", "；", "！", "？" }) do
        text = text:gsub(mark .. "\n*", mark .. "\n")
    end
    text = text:gsub("\n+$", "")
    return text
end

local function textInBox(text, box, is_footer)
    local size = box.font_size
    while true do
        local widget = TextBoxWidget:new{
            text = text, face = Font:getFace("cfont", size), width = box.width,
            alignment = "left", alignment_strict = true, auto_para_direction = true,
            line_height = is_footer and 0.25 or 0.65,
            fgcolor = is_footer and Blitbuffer.COLOR_WHITE or Blitbuffer.COLOR_BLACK,
            bgcolor = is_footer and Blitbuffer.COLOR_BLACK or Blitbuffer.COLOR_WHITE,
        }
        if widget:getSize().h <= box.height then return widget end
        widget:free()
        if size <= box.min_size then
            -- Rare oversized API fields must not spill into the stones/footer.
            return TextBoxWidget:new{
                text = text, face = Font:getFace("cfont", size), width = box.width,
                height = box.height, height_overflow_show_ellipsis = true,
                alignment = "left", alignment_strict = true, auto_para_direction = true,
                line_height = is_footer and 0.25 or 0.65,
                fgcolor = is_footer and Blitbuffer.COLOR_WHITE or Blitbuffer.COLOR_BLACK,
                bgcolor = is_footer and Blitbuffer.COLOR_BLACK or Blitbuffer.COLOR_WHITE,
            }
        end
        size = math.max(box.min_size, size - 2)
    end
end

function Wallpaper.render(plugin_path, payload, date, target, width, height)
    local image, background, eink
    local widgets = {}
    local tmp = target .. ".tmp"
    os.remove(tmp)
    local ok, result = pcall(function()
        local layout = Wallpaper.layout(width, height)
        background = RenderImage:renderImageFile(plugin_path .. "/assets/lyrics-background.png", false)
        assert(background, "Cannot load the bundled wallpaper background")
        background = RenderImage:scaleBlitBuffer(background, width, height)
        assert(background, "Cannot scale the wallpaper background")
        image = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
        image:blitFrom(background, 0, 0, 0, 0, width, height)
        background:free()
        background = nil
        -- Paint a precise black band after scaling so footer glyphs have a
        -- uniform background at every Kindle resolution.
        image:paintRect(0, layout.footer.y, width, layout.footer.height, Blitbuffer.COLOR_BLACK)
        local function paint(text, box, footer)
            local widget = textInBox(text, box, footer)
            widgets[#widgets + 1] = widget
            widget:paintTo(image, box.x, box.y)
        end
        paint(Wallpaper.body(payload), layout.body, false)
        paint(Wallpaper.source(payload), layout.source, true)
        paint(date:sub(1, 4) .. "-" .. date:sub(5, 6) .. "-" .. date:sub(7, 8) .. "  ·  一言", layout.date, true)
        eink = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
        eink:ditherblitFrom(image, 0, 0, 0, 0, width, height)
        image:free()
        image, eink = eink, nil
        local called, written, err = image:writeToFile(tmp, "png")
        assert(called and written ~= false, err or written or "Cannot write the wallpaper PNG")
        assert(Wallpaper.isValidPNG(tmp), "Incomplete wallpaper PNG")
        assert(os.rename(tmp, target), "Cannot replace the wallpaper PNG")
        return true
    end)
    for _, widget in ipairs(widgets) do widget:free() end
    if background then background:free() end
    if eink then eink:free() end
    if image then image:free() end
    if not ok then
        os.remove(tmp)
        logger.warn("Yiyan: wallpaper rendering failed", tostring(result))
        return false, tostring(result)
    end
    return result
end

return Wallpaper
