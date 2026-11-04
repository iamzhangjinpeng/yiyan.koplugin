local _ = require("gettext")

return {
    name = "yiyan",
    fullname = _("一言锁屏壁纸"),
    description = _("每天生成一张一言壁纸，并可自动配置 KOReader 屏保。"),
    version = "1.2.2",
    depends = {},
    device_support = { "Kindle" },
}
