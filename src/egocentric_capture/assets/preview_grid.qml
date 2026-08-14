import QtQuick 6.0
import org.freedesktop.gstreamer.Qt6GLVideoItem 1.0

Rectangle {
    id: root
    color: "#080a0c"
    property bool singleMode: false
    property string selectedCamera: "cam_a"

    GstGLQt6VideoItem {
        objectName: "mosaicVideo"
        anchors.fill: parent
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#303840"
        border.width: 1
    }

    Rectangle {
        visible: !root.singleMode
        width: 2
        height: parent.height
        anchors.horizontalCenter: parent.horizontalCenter
        color: "#303840"
    }

    Rectangle {
        visible: !root.singleMode
        width: parent.width
        height: 2
        anchors.verticalCenter: parent.verticalCenter
        color: "#303840"
    }

    Repeater {
        model: [
            { key: "cam_a", title: "相机 A", x: 0, y: 0 },
            { key: "cam_b", title: "相机 B", x: 0.5, y: 0 },
            { key: "cam_c", title: "相机 C", x: 0, y: 0.5 },
            { key: "cam_d", title: "相机 D", x: 0.5, y: 0.5 }
        ]

        delegate: Rectangle {
            required property var modelData
            visible: !root.singleMode || root.selectedCamera === modelData.key
            x: root.singleMode ? 8 : modelData.x * root.width + 8
            y: root.singleMode ? 8 : modelData.y * root.height + 8
            width: titleText.implicitWidth + 14
            height: 26
            color: "#b314181b"

            Text {
                id: titleText
                anchors.centerIn: parent
                text: modelData.title
                color: "#eef1f3"
                font.pixelSize: 13
            }
        }
    }
}
