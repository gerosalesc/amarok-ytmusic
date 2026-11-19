/*
 * YouTube Music Scriptable Service for Amarok 3.3+ (Qt 6 / QJSEngine)
 *
 * Bridges Amarok's Internet -> YouTube Music sidebar with the local amarok-ytmusic
 * Python daemon via atomic directory/file IPC (working around Amarok 3.3's missing
 * qt.network / Downloader bindings) and streams audio from http://127.0.0.1:18732/stream/<id>.
 */

Importer.loadQtBinding("qt.core");

var SERVICE_NAME = "YouTube Music";
var WEB_AUTH_URL = "http://127.0.0.1:18732";
var POLL_INTERVAL_MS = 50;
var REQUEST_TIMEOUT_MS = 20000;

var scriptDir = Amarok.Info.scriptPath();
// scriptPath() is typically ~/.var/app/org.kde.amarok/data/amarok/scripts/youtube_music
var ipcBaseDir = QDir.cleanPath(scriptDir + "/../../ytmusic_ipc");
var ipcReqDir = ipcBaseDir + "/req";
var ipcRespDir = ipcBaseDir + "/resp";

var reqCounter = 0;
var populateGeneration = 0;

function ensureIpcDirs() {
    var d = new QDir(ipcBaseDir);
    d.mkpath(ipcReqDir);
    d.mkpath(ipcRespDir);
}

/* Pure-JS UTF-8 -> Base64url encoder (QJSEngine has no window.btoa) */
var B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

function utf8Bytes(str) {
    var bytes = [];
    for (var i = 0; i < str.length; i++) {
        var code = str.charCodeAt(i);
        if (code < 0x80) {
            bytes.push(code);
        } else if (code < 0x800) {
            bytes.push(0xc0 | (code >> 6));
            bytes.push(0x80 | (code & 0x3f));
        } else if (code >= 0xd800 && code <= 0xdbff && i + 1 < str.length) {
            var next = str.charCodeAt(++i);
            var cp = 0x10000 + (((code - 0xd800) << 10) | (next - 0xdc00));
            bytes.push(0xf0 | (cp >> 18));
            bytes.push(0x80 | ((cp >> 12) & 0x3f));
            bytes.push(0x80 | ((cp >> 6) & 0x3f));
            bytes.push(0x80 | (cp & 0x3f));
        } else {
            bytes.push(0xe0 | (code >> 12));
            bytes.push(0x80 | ((code >> 6) & 0x3f));
            bytes.push(0x80 | (code & 0x3f));
        }
    }
    return bytes;
}

function toBase64Url(str) {
    if (!str) {
        return "";
    }
    var bytes = utf8Bytes(str);
    // Keep filename component comfortably under Linux's 255-byte limit
    if (bytes.length > 150) {
        bytes = bytes.slice(0, 150);
    }
    var out = "";
    for (var i = 0; i < bytes.length; i += 3) {
        var b0 = bytes[i];
        var has1 = (i + 1) < bytes.length;
        var has2 = (i + 2) < bytes.length;
        var b1 = has1 ? bytes[i + 1] : 0;
        var b2 = has2 ? bytes[i + 2] : 0;

        out += B64_CHARS.charAt(b0 >> 2);
        out += B64_CHARS.charAt(((b0 & 3) << 4) | (b1 >> 4));
        if (has1) {
            out += B64_CHARS.charAt(((b1 & 15) << 2) | (b2 >> 6));
        }
        if (has2) {
            out += B64_CHARS.charAt(b2 & 63);
        }
    }
    return out;
}

function decodeAmarokFilter(filter) {
    if (!filter) {
        return "";
    }
    try {
        return decodeURIComponent(filter).trim();
    } catch (e) {
        return String(filter).replace(/%20/g, " ").trim();
    }
}

function sendIpcRequest(op, payload, onResult) {
    ensureIpcDirs();
    reqCounter += 1;
    var reqId = String(Date.now()) + "_" + String(reqCounter);
    var encoded = toBase64Url(payload || "");
    var reqName = "r." + reqId + "." + op + "." + encoded;
    var respPath = ipcRespDir + "/" + reqId + ".json";

    var dir = new QDir(ipcReqDir);
    if (!dir.mkdir(reqName)) {
        onResult({ ok: false, error: "Could not create IPC request in " + ipcReqDir });
        return;
    }

    var startedAt = Date.now();

    function pollResponse() {
        var respFile = new QFile(respPath);
        if (respFile.exists()) {
            var content = "";
            if (respFile.open(QIODevice.ReadOnly)) {
                var ts = new QTextStream(respFile);
                content = ts.readAll();
                respFile.close();
            }
            respFile.remove();
            try {
                onResult(JSON.parse(content));
            } catch (err) {
                onResult({ ok: false, error: "Invalid JSON from daemon: " + String(err) });
            }
            return;
        }

        if ((Date.now() - startedAt) >= REQUEST_TIMEOUT_MS) {
            // Clean up unanswered request folder if daemon is not running
            var cleanup = new QDir(ipcReqDir);
            cleanup.rmdir(reqName);
            onResult({
                ok: false,
                timeout: true,
                error: "amarok-ytmusic daemon is not running. Start it and open " + WEB_AUTH_URL
            });
            return;
        }

        setTimeout(pollResponse, POLL_INTERVAL_MS);
    }

    setTimeout(pollResponse, POLL_INTERVAL_MS);
}

function insertFolderItem(name, callbackData, infoHtml, coverUrl) {
    var item = Amarok.StreamItem;
    item.level = 1;
    item.itemName = name || "Untitled";
    item.callbackData = callbackData || "info:empty";
    item.playableUrl = "";
    item.artist = "";
    item.album = "";
    item.genre = "";
    item.composer = "";
    item.year = 0;
    item.coverUrl = coverUrl || "";
    item.infoHtml = infoHtml || "";
    script.insertItem(item);
}

function insertTrackItem(entry) {
    var item = Amarok.StreamItem;
    item.level = 0;
    item.itemName = entry.itemName || "Untitled Track";
    item.callbackData = "";
    item.playableUrl = entry.playableUrl || "";
    item.artist = entry.artist || "Unknown Artist";
    item.album = entry.album || "YouTube Music";
    item.genre = "YouTube Music";
    item.composer = "";
    item.year = entry.year ? parseInt(entry.year, 10) : 0;
    item.coverUrl = entry.coverUrl || "";
    item.infoHtml = entry.infoHtml || "";
    script.insertItem(item);
}

function YouTubeMusicService() {
    var rootHtml =
        "<div style='font-family:sans-serif;padding:8px;'>" +
        "<h2>YouTube Music</h2>" +
        "<p>Stream your <b>Liked Music</b>, <b>Playlists</b>, <b>Library</b>, and live search results directly inside Amarok.</p>" +
        "<p>Manage Web Auth &amp; audio quality at <b>" + WEB_AUTH_URL + "</b> " +
        "(or click <i>Tools → YouTube Music: Sign In / Web Auth</i>).</p>" +
        "</div>";

    ScriptableServiceScript.call(
        this,
        SERVICE_NAME,
        2,
        "Stream your YouTube Music library and search results",
        rootHtml,
        true
    );
}

function onPopulating(level, callbackData, filter) {
    populateGeneration += 1;
    var gen = populateGeneration;
    var cleanFilter = decodeAmarokFilter(filter);
    Amarok.debug("YouTube Music populate: level=" + level + " callback=" + callbackData + " filter=" + cleanFilter);

    if (level === 1) {
        sendIpcRequest("folders", cleanFilter, function (resp) {
            if (gen !== populateGeneration) {
                return;
            }
            if (!resp || !resp.ok) {
                var errMsg = (resp && resp.error) ? resp.error : "Unknown daemon error";
                insertFolderItem(
                    "⚠ Daemon Offline — Start amarok-ytmusic",
                    "info:daemon_offline",
                    "<h3>YouTube Music Bridge Not Running</h3><p>" + errMsg + "</p>" +
                    "<p>Run <code>systemctl --user start amarok-ytmusic</code> or <code>amarok-ytmusic serve --open-auth</code>.</p>",
                    ""
                );
                script.donePopulating();
                return;
            }

            var items = resp.items || [];
            for (var i = 0; i < items.length; i++) {
                var f = items[i];
                insertFolderItem(f.itemName, f.callbackData, f.infoHtml, f.coverUrl);
            }
            script.donePopulating();
        });
        return;
    }

    if (level === 0) {
        if (callbackData === "info:not_signed_in" || callbackData === "info:auth_error") {
            sendIpcRequest("open_auth", "", function () {});
            Amarok.Window.Statusbar.longMessage("Opening YouTube Music Web Auth at " + WEB_AUTH_URL);
            script.donePopulating();
            return;
        }
        if (!callbackData || callbackData.indexOf("info:") === 0) {
            script.donePopulating();
            return;
        }

        sendIpcRequest("tracks", callbackData, function (resp) {
            if (gen !== populateGeneration) {
                return;
            }
            if (!resp || !resp.ok) {
                var errMsg = (resp && resp.error) ? resp.error : "Failed to load tracks";
                Amarok.Window.Statusbar.longMessage("YouTube Music: " + errMsg);
                script.donePopulating();
                return;
            }

            var tracks = resp.items || [];
            for (var i = 0; i < tracks.length; i++) {
                insertTrackItem(tracks[i]);
            }
            script.donePopulating();
        });
        return;
    }

    script.donePopulating();
}

function onOpenWebAuth() {
    sendIpcRequest("open_auth", "", function (resp) {
        if (resp && resp.ok) {
            Amarok.Window.Statusbar.shortMessage("Opened YouTube Music Web Auth in your browser.");
        } else {
            Amarok.alert(
                "Could not reach the amarok-ytmusic daemon.\n\n" +
                "Start it with:\n  systemctl --user start amarok-ytmusic\n\n" +
                "Then open: " + WEB_AUTH_URL,
                "warning"
            );
        }
    });
}

function onRefreshCache() {
    sendIpcRequest("refresh", "", function (resp) {
        if (resp && resp.ok) {
            Amarok.Window.Statusbar.shortMessage("YouTube Music cache cleared. Type or clear search to reload.");
        } else {
            Amarok.Window.Statusbar.shortMessage("YouTube Music daemon is offline.");
        }
    });
}

ensureIpcDirs();

var script = new YouTubeMusicService();
script.populate.connect(onPopulating);

if (Amarok.Window.addToolsMenu("ytmusic_web_auth", "YouTube Music: Sign In / Web Auth", "internet-services")) {
    Amarok.Window.ToolsMenu.ytmusic_web_auth.triggered.connect(onOpenWebAuth);
}
if (Amarok.Window.addToolsMenu("ytmusic_refresh", "YouTube Music: Refresh Library Cache", "view-refresh")) {
    Amarok.Window.ToolsMenu.ytmusic_refresh.triggered.connect(onRefreshCache);
}
