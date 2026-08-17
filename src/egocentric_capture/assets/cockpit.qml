import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Layouts 6.5
import org.freedesktop.gstreamer.Qt6GLVideoItem 1.0

Rectangle {
    id: root
    width: 1600
    height: 900
    color: "#02070a"
    clip: true

    readonly property color cyan: "#52f4df"
    readonly property color blue: "#5ab6ff"
    readonly property color lime: "#b8f166"
    readonly property color red: "#ff5f69"
    readonly property color amber: "#ffd36a"
    readonly property color leftHandColor: "#50d7e8"
    readonly property color rightHandColor: "#ffc857"
    readonly property color handWristColor: "#ff5f57"
    readonly property color handJointColor: "#f4f6f7"
    readonly property color textColor: "#eefbfa"
    readonly property color mutedColor: "#9eb3b4"
    readonly property int monoSize: Math.max(9, Math.round(height / 90))
    property string primaryCamera: "cam_a"
    readonly property string gestureCamera: "cam_a"
    readonly property real thumbnailStartRatio: 0.3148
    readonly property real thumbnailStepRatio: 0.125
    readonly property real thumbnailWidthRatio: 0.1195
    readonly property real thumbnailStripLeft: thumbnailStartRatio * width
    readonly property real thumbnailStripRight: (
        thumbnailStartRatio
        + thumbnailStepRatio * 2
        + thumbnailWidthRatio
    ) * width
    readonly property var cameraCatalog: [
        { camera: "cam_a", name: "CAM A / RGB" },
        { camera: "cam_b", name: "CAM B / RGB" },
        { camera: "cam_c", name: "CAM C / MONO" },
        { camera: "cam_d", name: "CAM D / DEPTH" }
    ]
    readonly property var auxiliaryCameras: {
        const cameras = []
        for (let index = 0; index < cameraCatalog.length; index += 1) {
            if (cameraCatalog[index].camera !== primaryCamera)
                cameras.push(cameraCatalog[index])
        }
        return cameras
    }
    readonly property var handLinks: [
        [0, 1], [1, 2], [2, 3], [3, 4],
        [0, 5], [5, 6], [6, 7], [7, 8],
        [5, 9], [9, 10], [10, 11], [11, 12],
        [9, 13], [13, 14], [14, 15], [15, 16],
        [13, 17], [17, 18], [18, 19], [19, 20],
        [0, 17]
    ]

    FontLoader {
        id: brandFont
        source: "fonts/Sora-Variable.ttf"
    }

    signal primaryCameraRequested(string camera)
    signal thumbnailHoverChanged(string camera, bool hovered)

    function selectPrimaryCamera(camera) {
        if (camera === root.primaryCamera)
            return
        root.primaryCameraRequested(camera)
    }

    function cameraViewport(camera) {
        if (camera === root.primaryCamera) {
            return {
                x: 0,
                y: 0,
                width: root.width,
                height: root.height,
                main: true
            }
        }
        let auxiliaryIndex = -1
        for (let index = 0; index < root.auxiliaryCameras.length; index += 1) {
            if (root.auxiliaryCameras[index].camera === camera) {
                auxiliaryIndex = index
                break
            }
        }
        if (auxiliaryIndex < 0)
            return { x: 0, y: 0, width: 0, height: 0, main: false }
        return {
            x: (
                root.thumbnailStartRatio
                + auxiliaryIndex * root.thumbnailStepRatio
            ) * root.width,
            y: 0.8556 * root.height,
            width: root.thumbnailWidthRatio * root.width,
            height: 0.1194 * root.height,
            main: false
        }
    }

    Image {
        anchors.fill: parent
        source: "cockpit_demo.png"
        fillMode: Image.PreserveAspectCrop
        visible: uiBridge.demoBackground
    }

    GstGLQt6VideoItem {
        id: mosaicVideo
        objectName: "mosaicVideo"
        anchors.fill: parent
        opacity: uiBridge.demoBackground ? 0 : 1
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#7601070a" }
            GradientStop { position: 0.18; color: "#1401070a" }
            GradientStop { position: 0.72; color: "#0501070a" }
            GradientStop { position: 1.0; color: "#a802070a" }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#182c31"
        border.width: 1
        opacity: 0.45
    }

    Canvas {
        id: videoHandOverlay
        objectName: "videoHandOverlay"
        anchors.fill: parent

        function drawHand(context, handData, lineColor, viewport) {
            const points = handData.landmarks || []
            if (points.length < 21 || viewport.width <= 0)
                return
            const stale = handData.staleMs || 0
            const fade = stale <= 250
                ? 1 : Math.max(0, 1 - (stale - 250) / 250)
            const alpha = fade * (viewport.main ? 0.92 : 0.68)
            const lineWidth = viewport.main
                ? Math.max(2.2, root.height / 360)
                : Math.max(1, viewport.height / 70)
            const jointRadius = viewport.main
                ? Math.max(2.8, root.height / 300)
                : Math.max(1.2, viewport.height / 62)
            const mapped = []
            for (let index = 0; index < points.length; index += 1) {
                mapped.push({
                    x: viewport.x + points[index].x * viewport.width,
                    y: viewport.y + points[index].y * viewport.height
                })
            }

            context.save()
            context.beginPath()
            context.rect(
                viewport.x,
                viewport.y,
                viewport.width,
                viewport.height
            )
            context.clip()
            context.globalAlpha = alpha
            context.lineCap = "round"
            context.lineJoin = "round"
            for (let pass = 0; pass < 2; pass += 1) {
                context.strokeStyle = pass === 0 ? "#b5000508" : lineColor
                context.lineWidth = pass === 0
                    ? lineWidth + (viewport.main ? 3 : 1.5)
                    : lineWidth
                for (let index = 0; index < root.handLinks.length; index += 1) {
                    const link = root.handLinks[index]
                    context.beginPath()
                    context.moveTo(mapped[link[0]].x, mapped[link[0]].y)
                    context.lineTo(mapped[link[1]].x, mapped[link[1]].y)
                    context.stroke()
                }
            }
            for (let index = 0; index < mapped.length; index += 1) {
                const point = mapped[index]
                const radius = index === 0
                    ? jointRadius * 1.65 : jointRadius
                context.fillStyle = "#d8000508"
                context.beginPath()
                context.arc(
                    point.x,
                    point.y,
                    radius + (viewport.main ? 1.7 : 0.7),
                    0,
                    Math.PI * 2
                )
                context.fill()
                context.fillStyle = index === 0
                    ? root.handWristColor : root.handJointColor
                context.beginPath()
                context.arc(point.x, point.y, radius, 0, Math.PI * 2)
                context.fill()
            }
            context.restore()
        }

        onPaint: {
            const context = getContext("2d")
            context.clearRect(0, 0, width, height)
            const viewport = root.cameraViewport(root.gestureCamera)
            drawHand(
                context,
                uiBridge.leftHand,
                root.leftHandColor,
                viewport
            )
            drawHand(
                context,
                uiBridge.rightHand,
                root.rightHandColor,
                viewport
            )
        }

        Connections {
            target: uiBridge
            function onChanged() {
                videoHandOverlay.requestPaint()
            }
        }
    }

    onPrimaryCameraChanged: videoHandOverlay.requestPaint()
    onWidthChanged: videoHandOverlay.requestPaint()
    onHeightChanged: videoHandOverlay.requestPaint()

    component CornerLine: Item {
        property bool rightSide: false
        property bool bottomSide: false
        width: 52
        height: 42

        Rectangle {
            width: parent.width
            height: 2
            color: root.cyan
            anchors.top: parent.top
            visible: !parent.bottomSide
        }
        Rectangle {
            width: parent.width
            height: 2
            color: root.cyan
            anchors.bottom: parent.bottom
            visible: parent.bottomSide
        }
        Rectangle {
            width: 2
            height: parent.height
            color: root.cyan
            anchors.left: parent.left
            visible: !parent.rightSide
        }
        Rectangle {
            width: 2
            height: parent.height
            color: root.cyan
            anchors.right: parent.right
            visible: parent.rightSide
        }
    }

    Item {
        id: edgeFrame
        anchors {
            top: parent.top
            topMargin: 92
            left: parent.left
            leftMargin: 22
            right: parent.right
            rightMargin: 22
            bottom: parent.bottom
            bottomMargin: 59
        }

        Rectangle {
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: root.cyan
            opacity: 0.72
        }
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            width: Math.max(
                0,
                root.thumbnailStripLeft - edgeFrame.x - 10
            )
            height: 1
            color: root.cyan
            opacity: 0.72
        }
        Rectangle {
            id: bottomRightEdge
            anchors.bottom: parent.bottom
            x: Math.max(
                0,
                root.thumbnailStripRight - edgeFrame.x + 10
            )
            width: Math.max(0, parent.width - x)
            height: 1
            color: root.cyan
            opacity: 0.72
        }
        CornerLine {
            anchors.left: parent.left
            anchors.top: parent.top
        }
        CornerLine {
            rightSide: true
            anchors.right: parent.right
            anchors.top: parent.top
        }
        CornerLine {
            bottomSide: true
            anchors.left: parent.left
            anchors.bottom: parent.bottom
        }
        CornerLine {
            rightSide: true
            bottomSide: true
            anchors.right: parent.right
            anchors.bottom: parent.bottom
        }

        Repeater {
            model: 15

            Rectangle {
                x: 0
                y: 18 + index * Math.max(22, (edgeFrame.height - 60) / 14)
                width: index % 4 === 0 ? 15 : 7
                height: 1
                color: root.cyan
                opacity: index % 4 === 0 ? 0.58 : 0.28
            }
        }

        Repeater {
            model: 15

            Rectangle {
                anchors.right: parent.right
                y: 18 + index * Math.max(22, (edgeFrame.height - 60) / 14)
                width: index % 4 === 0 ? 15 : 7
                height: 1
                color: root.blue
                opacity: index % 4 === 0 ? 0.58 : 0.28
            }
        }
    }

    Text {
        x: -58
        y: root.height * 0.53
        rotation: -90
        text: "HUAZHI AI // SENSOR FUSION BUS 01"
        color: root.cyan
        font.family: "monospace"
        font.pixelSize: 8
        opacity: 0.48
    }

    Text {
        anchors.right: parent.right
        anchors.rightMargin: -60
        y: root.height * 0.53
        rotation: 90
        text: "REAL-TIME MOTION INTELLIGENCE // BUS 02"
        color: root.blue
        font.family: "monospace"
        font.pixelSize: 8
        opacity: 0.48
    }

    Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter
        y: 97
        width: primaryViewText.implicitWidth + 30
        height: 22
        color: "#b0061116"
        border.color: "#4952f4df"
        border.width: 1

        Text {
            id: primaryViewText
            anchors.centerIn: parent
            text: "主视角 / PRIMARY · " + root.primaryCamera.toUpperCase()
            color: root.textColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
        }
    }

    Item {
        id: brand
        x: 20
        y: 10
        width: Math.min(500, root.width * 0.44)
        height: 70

        Item {
            id: brandWordmarkArea
            width: 195
            height: parent.height

            Text {
                x: 2
                y: 18
                text: "HUAZHI AI"
                color: "#c9000508"
                font.family: brandFont.status === FontLoader.Ready
                    ? brandFont.name : "Noto Sans Display"
                font.pixelSize: 25
                font.weight: Font.Bold
                font.letterSpacing: 0
            }

            Text {
                id: brandWordmark
                objectName: "brandWordmark"
                x: 0
                y: 15
                text: "HUAZHI AI"
                color: "#f3ffff"
                style: Text.Outline
                styleColor: "#d000080c"
                font.family: brandFont.status === FontLoader.Ready
                    ? brandFont.name : "Noto Sans Display"
                font.pixelSize: 25
                font.weight: Font.Bold
                font.letterSpacing: 0
            }
        }

        Rectangle {
            x: 201
            y: 15
            width: 1
            height: 39
            color: root.cyan
            opacity: 0.58
        }

        Column {
            x: 218
            y: 12
            spacing: 1

            Text {
                id: productName
                objectName: "productName"
                text: "EgoCentric"
                color: root.textColor
                style: Text.Outline
                styleColor: "#d000080c"
                font.family: brandFont.status === FontLoader.Ready
                    ? brandFont.name : "Noto Sans Display"
                font.pixelSize: 18
                font.weight: Font.DemiBold
                font.letterSpacing: 0
            }
            Text {
                text: "数据驾驶舱  /  DATA COCKPIT"
                color: "#c6f8f2"
                style: Text.Outline
                styleColor: "#d000080c"
                font.family: "monospace"
                font.pixelSize: 9
                font.letterSpacing: 0
            }
            Rectangle {
                width: 112
                height: 1
                color: root.blue
                opacity: 0.56
            }
        }
    }

    Button {
        id: drawerButton
        x: brand.x + brand.width + 10
        y: 28
        width: 38
        height: 34
        padding: 0
        background: Rectangle {
            color: drawerButton.hovered ? "#7a102329" : "#54101d22"
            border.color: "#6352f4df"
            radius: 3
        }
        contentItem: Item {
            Repeater {
                model: 3
                Rectangle {
                    x: 10
                    y: 9 + index * 6
                    width: 18
                    height: 1
                    color: root.cyan
                }
            }
        }
        onClicked: uiBridge.setDrawerOpen(!uiBridge.drawerOpen)
        ToolTip.visible: hovered
        ToolTip.text: "采集设置"
    }

    Row {
        anchors.right: parent.right
        anchors.rightMargin: 22
        y: 20
        spacing: 18

        Column {
            width: 260
            anchors.verticalCenter: parent.verticalCenter

            Text {
                anchors.right: parent.right
                text: uiBridge.sessionTitle
                color: root.textColor
                font.pixelSize: 12
                font.bold: true
            }
            Text {
                anchors.right: parent.right
                topPadding: 5
                text: uiBridge.stateMessage + " · " + uiBridge.aiStatus
                color: root.mutedColor
                font.family: "monospace"
                font.pixelSize: 9
            }
        }

        Button {
            id: recordButton
            width: 50
            height: 50
            enabled: uiBridge.recordEnabled || uiBridge.recording
            padding: 0
            background: Rectangle {
                radius: width / 2
                color: recordButton.down ? "#7f221e23" : "#61340e14"
                border.color: recordButton.enabled ? "#aaff5f69" : "#557f8991"
                border.width: 1
            }
            contentItem: Item {
                Rectangle {
                    width: uiBridge.recording ? 18 : 20
                    height: uiBridge.recording ? 18 : 20
                    radius: uiBridge.recording ? 3 : width / 2
                    anchors.centerIn: parent
                    color: recordButton.enabled ? root.red : "#7f8991"
                }
            }
            onClicked: uiBridge.toggleRecording()
            ToolTip.visible: hovered
            ToolTip.text: uiBridge.recording ? "停止录制" : "开始录制"
        }
    }

    component MetricRow: Item {
        id: metricRow
        property string labelText: ""
        property string valueText: ""
        property color dotColor: root.cyan
        property bool reverse: false
        width: 250
        height: Math.max(
            metricDot.height,
            metricLabel.implicitHeight,
            metricValue.implicitHeight
        )

        Rectangle {
            id: metricDot
            objectName: metricRow.reverse
                ? "rightMetricDot" : "leftMetricDot"
            width: 5
            height: 5
            radius: 3
            color: metricRow.dotColor
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: metricRow.reverse ? undefined : parent.left
            anchors.right: metricRow.reverse ? parent.right : undefined
        }
        Text {
            id: metricLabel
            text: metricRow.labelText
            color: root.mutedColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: metricRow.reverse ? undefined : metricDot.right
            anchors.right: metricRow.reverse ? metricDot.left : undefined
            anchors.leftMargin: metricRow.reverse ? 0 : 10
            anchors.rightMargin: metricRow.reverse ? 10 : 0
        }
        Text {
            id: metricValue
            text: metricRow.valueText
            color: root.textColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
            font.bold: true
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: metricRow.reverse ? undefined : metricLabel.right
            anchors.right: metricRow.reverse ? metricLabel.left : undefined
            anchors.leftMargin: metricRow.reverse ? 0 : 10
            anchors.rightMargin: metricRow.reverse ? 10 : 0
        }
    }

    component CockpitButton: Button {
        id: cockpitButton
        padding: 8
        background: Rectangle {
            color: cockpitButton.down
                ? "#3552f4df"
                : cockpitButton.hovered ? "#2c254148" : "#db122329"
            border.color: cockpitButton.enabled ? "#6b52f4df" : "#3c536064"
            radius: 3
        }
        contentItem: Text {
            text: cockpitButton.text
            color: cockpitButton.enabled ? root.textColor : "#71868a"
            font.pixelSize: 13
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    Column {
        x: 37
        y: Math.max(340, root.height * 0.38)
        width: 250
        spacing: 11

        MetricRow {
            labelText: "OAK-4P VISION"
            valueText: uiBridge.deviceMetrics.length > 0
                ? uiBridge.deviceMetrics[0].state : "CHECKING"
        }
        MetricRow {
            labelText: "EMG BAND"
            valueText: uiBridge.deviceMetrics.length > 5
                ? uiBridge.deviceMetrics[5].rate : "-- HZ"
        }
        MetricRow {
            labelText: "WRITE LINK"
            valueText: uiBridge.writeRateText
        }
        MetricRow {
            labelText: "FREE SPACE"
            valueText: uiBridge.storageText
            dotColor: root.amber
        }
    }

    Column {
        anchors.right: parent.right
        anchors.rightMargin: 37
        y: Math.max(340, root.height * 0.38)
        width: 250
        spacing: 11

        MetricRow {
            reverse: true
            labelText: "CAPTURE FPS"
            valueText: uiBridge.fpsText
        }
        MetricRow {
            reverse: true
            labelText: "SYNC ERROR"
            valueText: uiBridge.syncText
        }
        MetricRow {
            reverse: true
            labelText: "HAND CONFIDENCE"
            valueText: uiBridge.gestureConfidenceText
        }
        MetricRow {
            reverse: true
            labelText: "GESTURE"
            valueText: uiBridge.gestureLabel
            dotColor: root.amber
        }
    }

    component EmgPanel: Item {
        id: emgPanel
        property string title: ""
        property var channels: []
        property bool rightAligned: false
        property var colors: [
            root.cyan,
            root.blue,
            root.lime,
            root.amber
        ]

        Rectangle {
            anchors.fill: parent
            color: "#a4071217"
            opacity: 0.76
        }

        Rectangle {
            width: 1
            height: parent.height
            color: emgPanel.rightAligned ? root.blue : root.cyan
            anchors.right: emgPanel.rightAligned ? parent.right : undefined
            anchors.left: emgPanel.rightAligned ? undefined : parent.left
            opacity: 0.7
        }

        Row {
            anchors.top: parent.top
            anchors.topMargin: 11
            anchors.left: parent.left
            anchors.leftMargin: 14
            spacing: 9

            Text {
                text: emgPanel.title
                color: root.textColor
                font.family: "monospace"
                font.pixelSize: root.monoSize
            }
            Text {
                text: "4 CH · 250 HZ"
                color: root.mutedColor
                font.family: "monospace"
                font.pixelSize: root.monoSize
            }
        }

        Canvas {
            id: emgCanvas
            anchors.fill: parent
            anchors.topMargin: 28

            onPaint: {
                const context = getContext("2d")
                context.clearRect(0, 0, width, height)
                const count = 4
                const gap = height / count
                context.lineWidth = 1
                for (let row = 0; row <= count; row += 1) {
                    context.strokeStyle = "#1f9dcdca"
                    context.beginPath()
                    context.moveTo(0, row * gap)
                    context.lineTo(width, row * gap)
                    context.stroke()
                }
                for (let channel = 0; channel < count; channel += 1) {
                    const values = emgPanel.channels.length > channel
                        ? emgPanel.channels[channel] : []
                    if (values.length < 2)
                        continue
                    context.strokeStyle = emgPanel.colors[channel]
                    context.lineWidth = channel < 2 ? 1.45 : 1.05
                    context.beginPath()
                    for (let index = 0; index < values.length; index += 1) {
                        const x = index * width / (values.length - 1)
                        const base = (channel + 0.5) * gap
                        const y = base - values[index] * gap * 0.42
                        if (index === 0)
                            context.moveTo(x, y)
                        else
                            context.lineTo(x, y)
                    }
                    context.stroke()
                }
            }
        }

        Connections {
            target: uiBridge
            function onChanged() {
                emgCanvas.requestPaint()
            }
        }
    }

    EmgPanel {
        x: 32
        y: 108
        width: Math.min(350, root.width * 0.23)
        height: Math.min(190, root.height * 0.21)
        title: "左臂肌电 / EMG 01–04"
        channels: uiBridge.leftEmg
    }

    EmgPanel {
        anchors.right: parent.right
        anchors.rightMargin: 32
        y: 108
        width: Math.min(350, root.width * 0.23)
        height: Math.min(190, root.height * 0.21)
        title: "右臂肌电 / EMG 05–08"
        rightAligned: true
        channels: uiBridge.rightEmg
        colors: [root.blue, "#c69cff", "#ff7f68", "#d8f4f1"]
    }

    component HandPanel: Item {
        id: handPanel
        objectName: "handPanel_" + fallbackLabel
        property var handData: ({})
        property color lineColor: root.cyan
        property string fallbackLabel: ""

        Rectangle {
            anchors.fill: parent
            color: "#19040d11"
            border.color: Qt.rgba(
                handPanel.lineColor.r,
                handPanel.lineColor.g,
                handPanel.lineColor.b,
                0.24
            )
            border.width: 1
        }

        Rectangle {
            x: 0
            y: 0
            width: 42
            height: 2
            color: handPanel.lineColor
            opacity: 0.72
        }

        Rectangle {
            x: 0
            y: 0
            width: 2
            height: 26
            color: handPanel.lineColor
            opacity: 0.72
        }

        Text {
            x: 14
            y: 10
            text: handPanel.handData.gesture
                ? handPanel.handData.handedness + "_HAND / "
                    + handPanel.handData.gesture + " · "
                    + (handPanel.handData.confidence * 100).toFixed(1) + "%"
                : handPanel.fallbackLabel + " / NO HAND"
            color: root.mutedColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
        }

        Canvas {
            id: handCanvas
            objectName: "handCanvas_" + handPanel.fallbackLabel
            anchors.fill: parent
            anchors.topMargin: 24

            onPaint: {
                const context = getContext("2d")
                context.clearRect(0, 0, width, height)
                const points = handPanel.handData.landmarks || []
                if (points.length < 21)
                    return

                const sourceAspect = 16 / 9
                const sourcePoints = []
                let minX = points[0].x * sourceAspect
                let maxX = minX
                let minY = points[0].y
                let maxY = minY
                for (let index = 0; index < points.length; index += 1) {
                    const sourcePoint = {
                        x: points[index].x * sourceAspect,
                        y: points[index].y
                    }
                    sourcePoints.push(sourcePoint)
                    minX = Math.min(minX, sourcePoint.x)
                    maxX = Math.max(maxX, sourcePoint.x)
                    minY = Math.min(minY, sourcePoint.y)
                    maxY = Math.max(maxY, sourcePoint.y)
                }
                const sourceCenterX = (minX + maxX) / 2
                const sourceCenterY = (minY + maxY) / 2
                const spanX = Math.max(0.001, maxX - minX)
                const spanY = Math.max(0.001, maxY - minY)
                const target = {
                    x: 18,
                    y: 18,
                    width: Math.max(1, width - 36),
                    height: Math.max(1, height - 36)
                }
                const scale = Math.min(
                    target.width / spanX,
                    target.height / spanY
                )
                const displayPoints = []
                for (let index = 0; index < sourcePoints.length; index += 1) {
                    displayPoints.push({
                        x: target.x + target.width / 2
                            + (sourcePoints[index].x - sourceCenterX) * scale,
                        y: target.y + target.height / 2
                            + (sourcePoints[index].y - sourceCenterY) * scale
                    })
                }

                const stale = handPanel.handData.staleMs || 0
                context.globalAlpha = stale <= 250
                    ? 1 : Math.max(0, 1 - (stale - 250) / 250)
                context.lineCap = "round"
                context.lineJoin = "round"
                const lineWidth = Math.max(
                    2,
                    Math.min(width, height) / 82
                )
                for (let pass = 0; pass < 2; pass += 1) {
                    context.strokeStyle = pass === 0
                        ? "#bd000508" : handPanel.lineColor
                    context.lineWidth = pass === 0
                        ? lineWidth + 2.4 : lineWidth
                    for (let index = 0; index < root.handLinks.length; index += 1) {
                        const link = root.handLinks[index]
                        const first = displayPoints[link[0]]
                        const second = displayPoints[link[1]]
                        context.beginPath()
                        context.moveTo(first.x, first.y)
                        context.lineTo(second.x, second.y)
                        context.stroke()
                    }
                }
                const baseRadius = Math.max(
                    3.2,
                    Math.min(width, height) / 62
                )
                for (let index = 0; index < displayPoints.length; index += 1) {
                    const point = displayPoints[index]
                    const radius = index === 0
                        ? baseRadius * 1.5 : baseRadius
                    context.fillStyle = "#d5000508"
                    context.beginPath()
                    context.arc(
                        point.x,
                        point.y,
                        radius + 1.8,
                        0,
                        Math.PI * 2
                    )
                    context.fill()
                    context.fillStyle = index === 0
                        ? root.handWristColor : root.handJointColor
                    context.beginPath()
                    context.arc(point.x, point.y, radius, 0, Math.PI * 2)
                    context.fill()
                }
                context.globalAlpha = 1
            }
        }

        Connections {
            target: uiBridge
            function onChanged() {
                handCanvas.requestPaint()
            }
        }
    }

    HandPanel {
        x: 32
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 68
        width: Math.min(350, root.width * 0.23)
        height: Math.min(270, root.height * 0.28)
        handData: uiBridge.leftHand
        lineColor: root.leftHandColor
        fallbackLabel: "LEFT"
    }

    HandPanel {
        anchors.right: parent.right
        anchors.rightMargin: 32
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 68
        width: Math.min(350, root.width * 0.23)
        height: Math.min(270, root.height * 0.28)
        handData: uiBridge.rightHand
        lineColor: root.rightHandColor
        fallbackLabel: "RIGHT"
    }

    Item {
        id: targetBox
        visible: uiBridge.targetOverlay.visible === true
        x: uiBridge.targetOverlay.x * root.width
        y: uiBridge.targetOverlay.y * root.height
        width: uiBridge.targetOverlay.width * root.width
        height: uiBridge.targetOverlay.height * root.height

        Rectangle {
            anchors.fill: parent
            color: "transparent"
            border.color: root.lime
            border.width: 1
        }
        Rectangle {
            x: -12
            y: parent.height / 2
            width: 24
            height: 1
            color: root.lime
        }
        Rectangle {
            x: parent.width / 2
            y: -12
            width: 1
            height: 24
            color: root.lime
        }
        Rectangle {
            x: 0
            y: -25
            width: targetLabel.width + 16
            height: 22
            color: "#d018230c"

            Text {
                id: targetLabel
                anchors.centerIn: parent
                text: uiBridge.targetOverlay.label + "  "
                    + Number(uiBridge.targetOverlay.confidence).toFixed(3)
                color: "#edfbd8"
                font.family: "monospace"
                font.pixelSize: root.monoSize
            }
        }
    }

    Repeater {
        model: root.auxiliaryCameras

        delegate: Item {
            id: cameraThumbnail
            objectName: "cameraThumbnail_" + modelData.camera
            x: (
                root.thumbnailStartRatio
                + index * root.thumbnailStepRatio
            ) * root.width
            y: 0.8556 * root.height
            width: 0.1195 * root.width
            height: 0.1194 * root.height

            Rectangle {
                anchors.fill: parent
                color: "transparent"
                border.color: "#4f9fe4df"
                border.width: 1
            }
            Rectangle {
                anchors.fill: parent
                color: thumbnailMouse.containsMouse ? "#1752f4df" : "transparent"
                opacity: thumbnailMouse.containsMouse ? 0.32 : 0

                Behavior on opacity {
                    NumberAnimation { duration: 120 }
                }
            }
            Text {
                x: 7
                y: 6
                text: modelData.name
                color: root.textColor
                font.family: "monospace"
                font.pixelSize: Math.max(7, root.monoSize - 2)
            }
            Text {
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 6
                text: thumbnailMouse.containsMouse ? "设为主视角" : "AUX / 30%"
                color: thumbnailMouse.containsMouse ? root.lime : root.cyan
                font.family: "monospace"
                font.pixelSize: Math.max(7, root.monoSize - 2)
            }

            MouseArea {
                id: thumbnailMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor

                onEntered: root.thumbnailHoverChanged(
                    modelData.camera,
                    true
                )
                onExited: root.thumbnailHoverChanged(
                    modelData.camera,
                    false
                )
                onClicked: root.selectPrimaryCamera(modelData.camera)
            }
        }
    }

    Row {
        x: 22
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 25
        spacing: 16

        Text {
            text: "SESSION"
            color: root.mutedColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
        }
        Text {
            text: uiBridge.trialText
            color: root.textColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
            font.bold: true
        }
        Text {
            text: "QUEUE " + uiBridge.queueText
            color: root.mutedColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
        }
        Text {
            text: uiBridge.qualityText
            color: root.textColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
        }
    }

    Row {
        anchors.right: parent.right
        anchors.rightMargin: 22
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 24
        spacing: 12

        Text {
            text: uiBridge.durationText
            color: root.textColor
            font.family: "monospace"
            font.pixelSize: 12
        }
        Rectangle {
            width: 6
            height: 6
            radius: 3
            color: uiBridge.recording ? root.red : root.mutedColor
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            text: uiBridge.recording ? "REC / RAW + AI" : uiBridge.captureState
            color: uiBridge.recording ? root.red : root.mutedColor
            font.family: "monospace"
            font.pixelSize: root.monoSize
        }
    }

    Rectangle {
        id: drawerShade
        anchors.fill: parent
        visible: opacity > 0
        opacity: uiBridge.drawerOpen ? 0.36 : 0
        color: "#02080b"
        Behavior on opacity { NumberAnimation { duration: 180 } }

        MouseArea {
            anchors.fill: parent
            onClicked: uiBridge.setDrawerOpen(false)
        }
    }

    Rectangle {
        id: settingsDrawer
        width: Math.min(420, root.width * 0.36)
        height: parent.height
        x: uiBridge.drawerOpen ? 0 : -width
        color: "#f2071116"
        border.color: "#4f80dcd5"
        border.width: 1

        Behavior on x {
            NumberAnimation {
                duration: 220
                easing.type: Easing.OutCubic
            }
        }

        Flickable {
            anchors.fill: parent
            anchors.margins: 22
            contentHeight: drawerColumn.height
            clip: true

            Column {
                id: drawerColumn
                width: parent.width
                spacing: 14

                Row {
                    width: parent.width

                    Column {
                        width: parent.width - 44
                        spacing: 4
                        Text {
                            text: "CAPTURE CONFIGURATION"
                            color: root.textColor
                            font.pixelSize: 17
                            font.bold: true
                        }
                        Text {
                            text: uiBridge.stateMessage
                            color: root.mutedColor
                            font.family: "monospace"
                            font.pixelSize: root.monoSize
                        }
                    }

                    CockpitButton {
                        width: 38
                        height: 34
                        text: "×"
                        onClicked: uiBridge.setDrawerOpen(false)
                    }
                }

                Rectangle {
                    width: parent.width
                    height: 1
                    color: "#3b7faaa4"
                }

                TextField {
                    width: parent.width
                    placeholderText: "受试者，例如 P001"
                    text: uiBridge.participant
                    enabled: !uiBridge.metadataLocked
                    color: root.textColor
                    placeholderTextColor: root.mutedColor
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: parent.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                    onTextEdited: uiBridge.participant = text
                }
                TextField {
                    width: parent.width
                    placeholderText: "任务，例如 grasp_cup"
                    text: uiBridge.task
                    enabled: !uiBridge.metadataLocked
                    color: root.textColor
                    placeholderTextColor: root.mutedColor
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: parent.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                    onTextEdited: uiBridge.task = text
                }
                ComboBox {
                    id: handCombo
                    width: parent.width
                    model: [
                        { text: "双手", value: "both" },
                        { text: "右手", value: "right" },
                        { text: "左手", value: "left" }
                    ]
                    textRole: "text"
                    enabled: !uiBridge.metadataLocked
                    contentItem: Text {
                        leftPadding: 10
                        text: handCombo.displayText
                        color: root.textColor
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: handCombo.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                    Component.onCompleted: {
                        currentIndex = uiBridge.hand === "right"
                            ? 1 : uiBridge.hand === "left" ? 2 : 0
                    }
                    onActivated: uiBridge.hand = model[index].value
                }
                TextField {
                    width: parent.width
                    placeholderText: "操作人"
                    text: uiBridge.operator
                    enabled: !uiBridge.metadataLocked
                    color: root.textColor
                    placeholderTextColor: root.mutedColor
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: parent.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                    onTextEdited: uiBridge.operator = text
                }
                TextArea {
                    width: parent.width
                    height: 72
                    placeholderText: "可选备注"
                    text: uiBridge.notes
                    enabled: !uiBridge.metadataLocked
                    wrapMode: TextEdit.Wrap
                    color: root.textColor
                    placeholderTextColor: root.mutedColor
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: parent.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                    onTextChanged: uiBridge.notes = text
                }

                Text {
                    text: "DEVICE HEALTH"
                    color: root.mutedColor
                    font.family: "monospace"
                    font.pixelSize: root.monoSize
                }

                Repeater {
                    model: uiBridge.deviceMetrics

                    delegate: Row {
                        width: drawerColumn.width
                        spacing: 10

                        Rectangle {
                            width: 7
                            height: 7
                            radius: 4
                            color: modelData.healthy ? root.cyan : root.red
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            width: 105
                            text: modelData.label
                            color: root.textColor
                            font.family: "monospace"
                            font.pixelSize: root.monoSize
                        }
                        Text {
                            width: 78
                            text: modelData.state
                            color: modelData.healthy ? root.cyan : root.red
                            font.family: "monospace"
                            font.pixelSize: root.monoSize
                        }
                        Text {
                            text: modelData.rate
                            color: root.mutedColor
                            font.family: "monospace"
                            font.pixelSize: root.monoSize
                        }
                    }
                }

                Rectangle {
                    width: parent.width
                    height: 1
                    color: "#3b7faaa4"
                }

                Text {
                    text: "ADVANCED"
                    color: root.mutedColor
                    font.family: "monospace"
                    font.pixelSize: root.monoSize
                }
                TextField {
                    id: portField
                    width: parent.width
                    text: uiBridge.wearablePort
                    placeholderText: "手环串口"
                    enabled: !uiBridge.metadataLocked
                    color: root.textColor
                    placeholderTextColor: root.mutedColor
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: parent.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                }
                SpinBox {
                    id: baudrateBox
                    width: parent.width
                    from: 9600
                    to: 4000000
                    stepSize: 115200
                    value: uiBridge.wearableBaudrate
                    enabled: !uiBridge.metadataLocked
                    editable: true
                    contentItem: TextInput {
                        text: baudrateBox.textFromValue(
                            baudrateBox.value,
                            baudrateBox.locale
                        )
                        color: root.textColor
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        readOnly: !baudrateBox.editable
                        validator: baudrateBox.validator
                    }
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: baudrateBox.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                }
                SpinBox {
                    id: queueBox
                    width: parent.width
                    from: 1000
                    to: 200000
                    stepSize: 1000
                    value: uiBridge.writerQueueSize
                    enabled: !uiBridge.metadataLocked
                    editable: true
                    contentItem: TextInput {
                        text: queueBox.textFromValue(
                            queueBox.value,
                            queueBox.locale
                        )
                        color: root.textColor
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        readOnly: !queueBox.editable
                        validator: queueBox.validator
                    }
                    background: Rectangle {
                        color: "#e00a171c"
                        border.color: queueBox.activeFocus
                            ? root.cyan : "#68405b61"
                        radius: 3
                    }
                }
                CockpitButton {
                    width: parent.width
                    text: "应用高级设置"
                    enabled: !uiBridge.metadataLocked
                    onClicked: uiBridge.applyAdvancedSettings(
                        portField.text,
                        baudrateBox.value,
                        queueBox.value
                    )
                }

                Text {
                    width: parent.width
                    text: uiBridge.outputPath
                    color: root.mutedColor
                    font.family: "monospace"
                    font.pixelSize: root.monoSize
                    wrapMode: Text.WrapAnywhere
                }
                Item { width: 1; height: 22 }
            }
        }
    }

    Rectangle {
        id: toast
        visible: uiBridge.toastMessage.length > 0
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 72
        width: Math.min(620, toastText.implicitWidth + 36)
        height: 42
        color: "#e2111a1f"
        border.color: "#6e52f4df"
        radius: 4

        Text {
            id: toastText
            anchors.centerIn: parent
            text: uiBridge.toastMessage
            color: root.textColor
            font.pixelSize: 13
        }
    }

    Rectangle {
        anchors.fill: parent
        visible: uiBridge.finalizing
        color: "#e203090c"

        Column {
            anchors.centerIn: parent
            width: Math.min(620, parent.width * 0.55)
            spacing: 20

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "正在安全收尾"
                color: root.textColor
                font.pixelSize: 28
                font.bold: true
            }
            Text {
                width: parent.width
                text: uiBridge.finalizeMessage
                color: root.mutedColor
                font.pixelSize: 14
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }
            ProgressBar {
                width: parent.width
                value: uiBridge.finalizeProgress
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        visible: uiBridge.closeConfirmation
        color: "#b802070a"

        Rectangle {
            anchors.centerIn: parent
            width: 480
            height: 210
            color: "#f20a151a"
            border.color: "#6d52f4df"
            radius: 6

            Column {
                anchors.fill: parent
                anchors.margins: 26
                spacing: 20

                Text {
                    text: "当前采集仍在录制"
                    color: root.textColor
                    font.pixelSize: 21
                    font.bold: true
                }
                Text {
                    width: parent.width
                    text: "退出会安全停止设备，并将当前轮次标记为失败。"
                    color: root.mutedColor
                    font.pixelSize: 14
                    wrapMode: Text.WordWrap
                }
                Row {
                    anchors.right: parent.right
                    spacing: 12

                    CockpitButton {
                        text: "继续录制"
                        onClicked: uiBridge.cancelClose()
                    }
                    CockpitButton {
                        text: "停止并退出"
                        onClicked: uiBridge.confirmClose()
                    }
                }
            }
        }
    }
}
