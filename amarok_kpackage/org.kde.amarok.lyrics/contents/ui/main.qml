/****************************************************************************************
 * Copyright (c) 2017 Malte Veerman <malte.veerman@gmail.com>                           *
 * Copyright (c) 2026 amarok-ytmusic contributors                                       *
 *                                                                                      *
 * This program is free software; you can redistribute it and/or modify it under        *
 * the terms of the GNU General Public License as published by the Free Software        *
 * Foundation; either version 2 of the License, or (at your option) any later           *
 * version.                                                                             *
 *                                                                                      *
 * This program is distributed in the hope that it will be useful, but WITHOUT ANY      *
 * WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A      *
 * PARTICULAR PURPOSE. See the GNU General Public License for more details.             *
 *                                                                                      *
 * You should have received a copy of the GNU General Public License along with         *
 * this program.  If not, see <http://www.gnu.org/licenses/>.                           *
 ****************************************************************************************/

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import org.kde.amarok.qml 1.0 as AmarokQml
import org.kde.amarok.lyrics 1.0
import org.kde.kirigami 2.14 as Kirigami

AmarokQml.Applet {
    id: applet

    readonly property string ytBridgeBase: "http://127.0.0.1:18732"
    property bool autoScroll: true
    property string ytVideoId: ""
    property string ytLyrics: ""
    property string _fetchedLyricsVid: ""

    configDialog: ConfigDialog {}

    function fetchYtLyricsFor(vid) {
        if (!vid || _fetchedLyricsVid === vid) {
            return;
        }
        _fetchedLyricsVid = vid;
        var xhr = new XMLHttpRequest();
        xhr.open("GET", ytBridgeBase + "/api/tracks/" + encodeURIComponent(vid) + "/lyrics");
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE || xhr.status !== 200) {
                return;
            }
            if (applet.ytVideoId !== vid) {
                return;
            }
            try {
                var res = JSON.parse(xhr.responseText);
                if (res && res.lyrics) {
                    var txt = res.lyrics;
                    if (res.source) {
                        txt = txt + "\n\n— " + res.source;
                    }
                    applet.ytLyrics = txt;
                }
            } catch (e) {
                // Ignore parse error
            }
        };
        xhr.send();
    }

    function pollYtLyrics() {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", ytBridgeBase + "/api/now-playing");
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) {
                return;
            }
            if (xhr.status !== 200) {
                applet.ytVideoId = "";
                applet.ytLyrics = "";
                return;
            }
            try {
                var data = JSON.parse(xhr.responseText);
                if (!data || !data.is_ytmusic || !data.video_id) {
                    applet.ytVideoId = "";
                    applet.ytLyrics = "";
                    applet._fetchedLyricsVid = "";
                    return;
                }
                if (applet.ytVideoId !== data.video_id) {
                    applet.ytVideoId = data.video_id;
                    applet.ytLyrics = "";
                    applet._fetchedLyricsVid = "";
                }
                if (data.lyrics) {
                    var txt = data.lyrics;
                    if (data.lyrics_source) {
                        txt = txt + "\n\n— " + data.lyrics_source;
                    }
                    applet.ytLyrics = txt;
                    applet._fetchedLyricsVid = data.video_id;
                } else {
                    applet.fetchYtLyricsFor(data.video_id);
                }
            } catch (e) {
                // Ignore
            }
        };
        xhr.send();
    }

    Component.onCompleted: pollYtLyrics()

    Timer {
        id: ytLyricsTimer
        interval: 4000
        repeat: true
        running: true
        onTriggered: applet.pollYtLyrics()
    }

    Loader {
        id: loader

        width: parent.width
        height: parent.height

        sourceComponent: LyricsEngine.text === "" && applet.ytLyrics === "" && LyricsEngine.suggestions.length > 0 ? suggestionsComponent : textComponent

        BusyIndicator {
            anchors.centerIn: parent
            running: LyricsEngine.fetching && applet.ytLyrics === ""
        }
    }

    RowLayout {
        id: buttonRow

        y: applet.spacing
        x: LyricsEngine.alignment === TextEdit.AlignRight ? applet.spacing : parent.width - width - applet.spacing - Kirigami.Units.largeSpacing * 2

        Button {
            Layout.alignment: Qt.AlignRight
            checkable: true
            checked: applet.autoScroll
            icon.name: "arrow-down"

            ToolTip.text: i18n("Toggle auto scroll")
            ToolTip.visible: hovered
            ToolTip.delay: Qt.styleHints.mousePressAndHoldInterval
            hoverEnabled: true

            onClicked: applet.autoScroll = !applet.autoScroll
        }
        Button {
            Layout.alignment: Qt.AlignRight
            icon.name: "view-refresh"
            ToolTip.text: i18n("Reload lyrics")
            ToolTip.visible: hovered
            ToolTip.delay: Qt.styleHints.mousePressAndHoldInterval
            hoverEnabled: true

            onClicked: {
                applet._fetchedLyricsVid = "";
                applet.pollYtLyrics();
                LyricsEngine.refetchLyrics();
            }
        }
    }

    Component {
        id: textComponent

        ScrollView {
            id: textView
            anchors.fill: parent

            TextArea {
                id: textArea

                text: LyricsEngine.text !== "" ? LyricsEngine.text : applet.ytLyrics
                font.pointSize: LyricsEngine.fontSize
                font.family: LyricsEngine.font
                wrapMode: TextEdit.Wrap
                horizontalAlignment: LyricsEngine.alignment
                verticalAlignment: TextEdit.AlignTop
                textFormat: LyricsEngine.text !== "" ? TextEdit.AutoText : TextEdit.PlainText
                readOnly: true

                Connections {
                    target: LyricsEngine
                    function onPositionChanged() {
                        if (applet.autoScroll) {
                            var middle = textArea.contentHeight * LyricsEngine.position;
                            var top = middle - textView.height / 2;
                            var maxTop = textArea.contentHeight - textView.height + Kirigami.Units.largeSpacing;
                            var boundTop = Math.min(maxTop, Math.max(0, top));
                            textView.contentItem.contentY = boundTop;
                        }
                    }
                }


                Label {
                    anchors.centerIn: parent
                    text: i18n("No lyrics found")
                    color: Kirigami.Theme.textColor
                    visible: !textArea.text
                    font.pointSize: LyricsEngine.fontSize
                    font.family: LyricsEngine.font
                }
            }
        }
    }

    // untested
    Component {
        id: suggestionsComponent

        ListView {
            model: LyricsEngine.suggestions
            header: Label {
                text: i18n("Suggestions: ") + LyricsEngine.suggestions.length
            }
            delegate: Item {
                width: parent.width

                Column {
                    width: parent.width

                    Label {
                        text: '<b>' + i18n("Title:") + '</b> ' + modelData[0]
                    }
                    Label {
                        text: '<b>' + i18n("Artist:") + '</b> ' + modelData[1]
                    }
                }
                MouseArea {
                    anchors.fill: parent

                    onClicked: LyricsEngine.fetchLyrics(modelData[0], modelData[1], modelData[2])
                }
            }
        }
    }
}
