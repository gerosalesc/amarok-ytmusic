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
import org.kde.kirigami 2.14 as Kirigami
import org.kde.amarok.qml 1.0 as AmarokQml
import org.kde.amarok.currenttrack 1.0

AmarokQml.Applet {
    id: applet

    // Local amarok-ytmusic daemon bridge for Ampache YouTube Music tracks
    readonly property string ytBridgeBase: "http://127.0.0.1:18732"
    property bool ytActive: false
    property string ytVideoId: ""
    property string ytSource: ""
    property string ytTitle: ""
    property string ytArtist: ""
    property string ytAlbum: ""
    property string ytCoverUrl: ""
    property int ytRating: 0
    property string ytPlayCount: "0"
    property string ytScore: "0"
    property string ytLastPlayed: "Never"
    property int _ytReqSeq: 0
    property double _lastRateAt: 0

    function resetYtState() {
        ytActive = false;
        ytVideoId = "";
        ytSource = "";
        ytTitle = "";
        ytArtist = "";
        ytAlbum = "";
        ytCoverUrl = "";
        ytRating = 0;
        ytPlayCount = "0";
        ytScore = "0";
        ytLastPlayed = "Never";
    }

    function fetchYtNowPlaying() {
        if (CurrentTrackEngine.track === "") {
            resetYtState();
            return;
        }
        var seq = ++_ytReqSeq;
        var xhr = new XMLHttpRequest();
        xhr.open("GET", ytBridgeBase + "/api/now-playing");
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) {
                return;
            }
            if (seq !== _ytReqSeq) {
                return;
            }
            if (xhr.status !== 200) {
                // Daemon offline or unavailable: seamlessly fall back to stock CurrentTrackEngine
                resetYtState();
                return;
            }
            try {
                var data = JSON.parse(xhr.responseText);
                if (!data || !data.is_ytmusic || !data.video_id || CurrentTrackEngine.track === "") {
                    resetYtState();
                    return;
                }
                var sameTrack = (ytVideoId === data.video_id);
                ytActive = true;
                ytVideoId = data.video_id;
                ytSource = data.source || "ampache";
                ytTitle = data.title || CurrentTrackEngine.track;
                ytArtist = data.artist || CurrentTrackEngine.artist;
                ytAlbum = data.album || CurrentTrackEngine.album;

                var fullCover = data.cover_url_full || "";
                if (fullCover !== "" && fullCover !== ytCoverUrl) {
                    ytCoverUrl = fullCover;
                } else if (!sameTrack && fullCover === "") {
                    ytCoverUrl = "";
                }

                // Preserve optimistic star rating briefly while POST /rate finishes
                var nowMs = Date.now();
                if (!sameTrack || (nowMs - _lastRateAt) > 2500) {
                    ytRating = (data.amarok_rating !== undefined) ? data.amarok_rating : 0;
                }

                ytPlayCount = String((data.play_count !== undefined) ? data.play_count : 0);
                ytScore = String((data.score_display !== undefined) ? data.score_display : Math.round(data.score || 0));
                ytLastPlayed = data.last_played_display || "Never";
            } catch (e) {
                resetYtState();
            }
        };
        xhr.send();
    }

    function rateYtTrack(newRating) {
        if (!ytActive || ytVideoId === "") {
            return;
        }
        var clamped = Math.max(0, Math.min(10, parseInt(newRating, 10) || 0));
        ytRating = clamped;
        _lastRateAt = Date.now();

        var vid = ytVideoId;
        var xhr = new XMLHttpRequest();
        xhr.open("POST", ytBridgeBase + "/api/tracks/" + encodeURIComponent(vid) + "/rate");
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE || xhr.status !== 200) {
                return;
            }
            if (ytVideoId !== vid) {
                return;
            }
            try {
                var res = JSON.parse(xhr.responseText);
                if (res && res.amarokRating !== undefined) {
                    ytRating = res.amarokRating;
                }
                if (res && res.playCount !== undefined) {
                    ytPlayCount = String(res.playCount);
                }
                if (res && res.scoreDisplay !== undefined) {
                    ytScore = String(res.scoreDisplay);
                }
                if (res && res.lastPlayedDisplay) {
                    ytLastPlayed = res.lastPlayedDisplay;
                }
            } catch (e) {
                // Keep optimistic rating
            }
        };
        xhr.send(JSON.stringify({ amarok_rating: clamped }));
    }

    Component.onCompleted: fetchYtNowPlaying()

    Connections {
        target: CurrentTrackEngine
        function onTrackChanged() {
            ytRefreshDelay.restart();
            applet.fetchYtNowPlaying();
        }
        function onAlbumChanged() {
            applet.fetchYtNowPlaying();
        }
    }

    // Short follow-up poll 1.2s after track change so freshly-fetched metadata/cover appear right away
    Timer {
        id: ytRefreshDelay
        interval: 1200
        repeat: false
        onTriggered: applet.fetchYtNowPlaying()
    }

    // Periodic refresh while playing so Play Count, Score, and Last played update live at 15s & track progress
    Timer {
        id: ytLivePoll
        interval: 4000
        repeat: true
        running: CurrentTrackEngine.track !== ""
        onTriggered: applet.fetchYtNowPlaying()
    }

    // album art
    Loader {
        id: cover

        height: parent.height
        width: height

        sourceComponent: (applet.ytActive && applet.ytCoverUrl !== "")
            ? ytCoverComponent
            : (CurrentTrackEngine.hasValidCover ? coverComponent : emptyComponent)
    }

    ColumnLayout {
        anchors {
            left: cover.right
            leftMargin: applet.spacing
            right: parent.right
            top: parent.top
            bottom: parent.bottom
        }

        GridLayout {
            id: artistRatingGrid
            Layout.preferredWidth: parent.width
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.alignment: Qt.AlignTop
            flow: parent.width < ( infoItem.Layout.minimumWidth + ratingItem.Layout.minimumWidth ) * 8 ? GridLayout.TopToBottom : GridLayout.LeftToRight

            InfoItem {
                id: infoItem

                ytActive: applet.ytActive
                ytTitle: applet.ytTitle
                ytArtist: applet.ytArtist
                ytAlbum: applet.ytAlbum

                width: ( artistRatingGrid.flow == GridLayout.TopToBottom ? parent.width : parent.width / 2 )
                Layout.minimumWidth: 24
                Layout.maximumWidth: parent.width
                Layout.fillHeight: true
                Layout.alignment: Qt.AlignLeft
            }
            AmarokQml.RatingItem {
                id: ratingItem

                Layout.preferredWidth: height * 6
                Layout.preferredHeight: parent.height / 5
                Layout.maximumHeight: parent.height / 5
                Layout.minimumWidth: 24
                Layout.maximumWidth: parent.width / ( artistRatingGrid.flow == GridLayout.TopToBottom ? 1 : 2 )
                Layout.minimumHeight: 100

                Layout.alignment: Qt.AlignTop | Qt.AlignRight
                rating: applet.ytActive ? applet.ytRating : CurrentTrackEngine.rating
                onClicked: {
                    if (applet.ytActive && applet.ytVideoId !== "") {
                        applet.rateYtTrack(newRating);
                        if (applet.ytSource === "local") {
                            CurrentTrackEngine.rating = newRating;
                        }
                    } else {
                        CurrentTrackEngine.rating = newRating;
                    }
                }
            }
        }
        StatsItem {
            id: statsItem

            ytActive: applet.ytActive
            ytPlayCount: applet.ytPlayCount
            ytScore: applet.ytScore
            ytLastPlayed: applet.ytLastPlayed

            Layout.fillWidth: true
            Layout.alignment: Qt.AlignBottom
            Layout.preferredHeight: parent.height / 5
        }
        visible: CurrentTrackEngine.track !== ""
    }

    RowLayout {
        anchors {
            left: cover.right
            leftMargin: applet.spacing
            right: parent.right
            top: parent.top
            bottom: parent.bottom
        }
        Label {
            id: nothingPlaying
            font.bold: true
            color: Kirigami.Theme.textColor
            Layout.alignment: Qt.AlignCenter
            Layout.minimumWidth: parent.height / 3
            text: i18n("No track playing")
        }
        Item {
            id: logoComponent
            Layout.minimumWidth: parent.height / 3
            Layout.maximumWidth: parent.height
            Layout.fillHeight: true
            AmarokQml.PixmapItem {
                id: amarokLogo
                source: Svg.renderSvg(applet.imageUrl("amarok-currenttrack.svg"),
                                    "CurrentTrackLogo",
                                    512,
                                    512,
                                    "amarok_logo");
                width: parent.width
                height: width
                anchors.centerIn: parent
            }
        }
        visible: CurrentTrackEngine.track === ""
    }

    Component {
        id: ytCoverComponent

        Rectangle {
            id: ytCoverRect

            color: "white"
            radius: Kirigami.Units.smallSpacing / 2
            border.width: ytCoverMouse.containsMouse ? 3 : 1
            border.color: ytCoverMouse.containsMouse ? Kirigami.Theme.highlightColor : applet.palette.light

            Image {
                id: ytCoverImage

                anchors.fill: parent
                anchors.margins: parent.radius
                source: applet.ytCoverUrl
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                smooth: true
                mipmap: true
                onStatusChanged: {
                    if (status === Image.Error) {
                        applet.ytCoverUrl = "";
                    }
                }
            }
            MouseArea {
                id: ytCoverMouse
                anchors.fill: parent
                hoverEnabled: true
                onClicked: CurrentTrackEngine.displayCover()
            }
        }
    }

    Component {
        id: coverComponent

        Rectangle {
            id: cover

            color: "white"
            radius: Kirigami.Units.smallSpacing / 2
            border.width: albumCoverMouse.containsMouse ? 3 : 1
            border.color: albumCoverMouse.containsMouse ? Kirigami.Theme.highlightColor : applet.palette.light

            AmarokQml.PixmapItem {
                id: iconItem

                anchors.fill: parent
                anchors.margins: parent.radius
                source: CurrentTrackEngine.cover
            }
            MouseArea {
                id: albumCoverMouse
                anchors.fill: parent
                hoverEnabled: true
                onClicked: CurrentTrackEngine.displayCover()
            }
        }
    }
    Component {
        id: emptyComponent

        AmarokQml.PixmapItem {
            source: Svg.renderSvg(applet.imageUrl("amarok-currenttrack.svg"),
                                  "CurrentTrackAlbum",
                                  512,
                                  512,
                                  "album_old");
            width: parent.width
            height: parent.height
        }
    }
}
