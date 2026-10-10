import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.UPower
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "lukedaduke.power"
  ipcTarget: "lukedaduke.power"
  // manageIpc: false so this panel can own the single IpcHandler the target
  // permits — needed for the togglePercentage method below.
  manageIpc: false
  property var powerData: ({})
  property var batteryInfo: ({})
  property var profiles: []
  property string activeProfile: ""
  property int profileIndex: 0
  property bool cursorActive: false

  // Precise kernel readings from battery_helper.py, separate from batteryInfo
  // (which is omarchy's coarse display string) so the panel can show the
  // underlying numbers rather than re-parsing formatted text.
  readonly property var tele: (root.powerData && root.powerData.telemetry) || ({})
  readonly property bool haveTele: root.tele && root.tele.energy_wh !== undefined
  readonly property bool showPercentage: setting("showPercentage", false) === true
  readonly property bool batteryPresent: {
    var device = UPower.displayDevice
    return !!(device && device.isPresent)
  }

  function upowerStates() {
    return {
      Charging: UPowerDeviceState.Charging,
      Discharging: UPowerDeviceState.Discharging,
      FullyCharged: UPowerDeviceState.FullyCharged,
      PendingCharge: UPowerDeviceState.PendingCharge
    }
  }

  function selectProfileByDelta(delta) {
    profileIndex = Model.selectProfileIndex(profileIndex, delta, profiles)
  }

  function activateSelectedProfile() {
    if (profileIndex < 0 || profileIndex >= profiles.length) return
    setProfile(profiles[profileIndex])
  }

  function batteryIcon() {
    var device = UPower.displayDevice
    return Model.batteryIcon(device, root.discharging, upowerStates())
  }

  function modeLabel() {
    var device = UPower.displayDevice
    return Model.modeLabel(device, root.discharging, upowerStates())
  }

  // ---- precise readouts -------------------------------------------------
  // Each prefers the kernel's own number and falls back to omarchy's coarse
  // string, so the panel degrades to what it showed before rather than blank.

  // 0..1 for the health bar; a brand-new pack reads 1.0.
  readonly property real healthFraction: {
    var n = Number(root.tele.health_pct)
    if (!isFinite(n) || n <= 0) return 0
    return Math.max(0, Math.min(1, n / 100))
  }

  readonly property bool healthDegraded: {
    var n = Number(root.tele.health_pct)
    return isFinite(n) && n > 0 && n < 80
  }

  // Big number only — the verdict and the cycles live in the caption.
  readonly property string healthHeadline: {
    var n = Number(root.tele.health_pct)
    return isFinite(n) && n > 0 ? n.toFixed(1) + "%" : Model.DASH
  }

  readonly property string healthCaption: {
    var bits = []
    if (root.tele.health) bits.push(root.tele.health)
    var cycles = root.tele.cycle_count
    if (typeof cycles === "number") bits.push(cycles + " cycles")
    return bits.length > 0 ? bits.join(" · ") : Model.DASH
  }

  // The rare-but-real numbers, on one dim line instead of three label rows.
  readonly property string packFooter: {
    var bits = []
    var limit = Model.fmtLimit(root.tele.charge_limit_pct, root.tele.charge_resume_pct)
    if (limit !== Model.DASH) bits.push(limit)
    var made = Model.fmtAge(root.tele.manufactured)
    if (made !== Model.DASH) bits.push("made " + made)
    if (root.tele.model) bits.push(root.tele.model)
    var ah = root.tele.charge_throughput_ah
    // "lifetime" is implied by sitting under a cycle count; spelling it out
    // pushed this line past the panel width and cost us the value.
    if (typeof ah === "number") bits.push(ah.toFixed(1) + " Ah")
    // Peripheral batteries ride the footer: the line already wraps, so a long
    // device name lands on a second row instead of overflowing its column.
    var periph = Model.fmtPeripherals(root.tele.peripherals)
    if (periph) bits.push(periph)
    return bits.join(" · ")
  }

  function energyText() {
    return Model.fmtEnergy(root.tele.energy_wh, root.tele.energy_full_wh)
  }

  function timeText() {
    // tte/ttf are kernel seconds and move every tick; omarchy's string is the
    // fallback when the helper has not reported yet.
    var secs = root.discharging ? root.tele.time_to_empty_s : root.tele.time_to_full_s
    if (typeof secs === "number" && secs > 0) return Model.fmtRuntime(secs)
    return root.batteryInfo.time || Model.DASH
  }

  function powerText() {
    return Model.fmtPower(root.tele.power_w, root.tele.flow)
  }

  function tempText() {
    return Model.fmtTemp(root.tele.temp_c)
  }

  function profileIcon(name) {
    return Model.profileIcon(name)
  }

  readonly property bool fullyCharged: {
    var device = UPower.displayDevice
    return device && device.isPresent && device.state === UPowerDeviceState.FullyCharged && !root.chargeThresholdActive
  }
  readonly property bool discharging: {
    var device = UPower.displayDevice
    return !!(device && device.isPresent && UPower.onBattery)
  }
  readonly property bool chargeThresholdActive: {
    var device = UPower.displayDevice
    return Model.chargeThresholdActive(device, root.discharging, upowerStates())
  }
  readonly property bool batteryFull: fullyCharged || (!root.discharging && batteryFraction >= 1)
  readonly property bool batteryFlowIdle: batteryFull || chargeThresholdActive

  // 0..1 charge level, used by the visual progress bar.
  readonly property real batteryFraction: {
    var d = UPower.displayDevice
    return Model.batteryFraction(d)
  }

  readonly property bool charging: {
    var d = UPower.displayDevice
    return d && d.isPresent && !UPower.onBattery && !root.batteryFlowIdle
  }

  readonly property color batteryFillColor: {
    return root.bar ? root.bar.foreground : Color.foreground
  }

  // Cute agent-flavored phrases shown in the hero status line, rotated on a
  // timer so the panel feels alive when current is flowing (either direction).
  readonly property var chargingPhrases: [
    "Pumping power",
    "Injecting electrons",
    "Pouring juice",
    "Amassing watts",
    "Hoarding joules",
    "Sucking volts",
    "Topping reserves",
    "Soaking amps",
    "Inhaling kilowatts"
  ]
  readonly property var onBatteryPhrases: [
    "Slurping power",
    "Spending joules",
    "Draining watts",
    "Burning electrons",
    "Sipping juice",
    "Spending coulombs",
    "Bleeding amps",
    "Guzzling volts",
    "Munching reserves"
  ]
  property int phraseIndex: 0

  // Whichever list is "active" given the current power state.
  readonly property var activePhrases: {
    if (fullyCharged) return []
    if (charging) return chargingPhrases
    if (discharging) return onBatteryPhrases
    return []
  }
  readonly property bool rotatingPhrases: activePhrases.length > 0

  readonly property string heroStatusText: {
    if (fullyCharged) return "Fully charged"
    if (rotatingPhrases) return activePhrases[phraseIndex % activePhrases.length]
    return modeLabel()
  }

  // Absolute tool paths and no shell: a PATH-preceding shadow binary or a
  // hostile PATH must never run inside this long-lived shell process.
  // HOME/XDG_STATE_HOME stay in the fixed env: omarchy-powerprofiles-set
  // persists profile choice under $HOME/.local/state/omarchy/powerprofiles
  // and battery_helper.py writes ~/.local/state/omarchy/battery_history.json.
  readonly property string py: "/usr/bin/python3"
  readonly property string pluginRoot: {
    var p = Qt.resolvedUrl(".").toString()
    if (p.indexOf("file://") === 0)
      p = p.substring(7)
    if (p.length > 1 && p.charAt(p.length - 1) === "/")
      p = p.substring(0, p.length - 1)
    return p
  }
  readonly property var procEnv: ({
    "PATH": "/usr/bin:/bin",
    "HOME": Quickshell.env("HOME"),
    "XDG_STATE_HOME": Quickshell.env("XDG_STATE_HOME"),
    "XDG_RUNTIME_DIR": Quickshell.env("XDG_RUNTIME_DIR"),
    "LANG": null,
    "LC_ALL": "C"
  })

  function refresh() {
    refreshTelemetry()
    refreshProfiles()
  }

  // Volatile readings: charge, power, drain. Every panel tick used to respawn
  // all three helpers, but the active profile changes on a human timescale —
  // polling it alongside the battery just burned a process spawn per tick.
  function refreshTelemetry() {
    if (!batteryPresent) return

    if (!batteryProc.running) { batteryProc.running = true; batteryDeadline.restart() }
    if (!powerDataProc.running) { powerDataProc.running = true; powerDataDeadline.restart() }
  }

  function refreshProfiles() {
    if (!batteryPresent) return

    if (!profilesProc.running) { profilesProc.running = true; profilesDeadline.restart() }
  }

  function updateKeyValue(raw) {
    var next = Model.parseKeyValue(raw)
    // Keep last known good data if a refresh briefly returns nothing — happens
    // around AC plug/unplug events. Avoids the section collapsing mid-transition.
    if (Object.keys(next).length === 0) return
    batteryInfo = next
  }

  function updateProfiles(raw) {
    var parsed = Model.parseProfiles(raw, profileIndex)
    // Same guard as battery: preserve the last known profile list across
    // transient empty payloads so the buttons don't blink out.
    if (parsed.profiles.length === 0) return
    profiles = parsed.profiles
    activeProfile = parsed.activeProfile
    profileIndex = parsed.profileIndex
    if (opened && !cursorActive) {
      var idx = profiles.indexOf(activeProfile)
      if (idx >= 0) profileIndex = idx
    }
  }

  function setProfile(profile) {
    if (!profile || actionProc.running) return
    actionProc.command = ["/usr/bin/omarchy-powerprofiles-set", root.discharging ? "battery" : "ac", profile]
    actionDeadline.restart()
    actionProc.running = true
  }

  function togglePercentage() {
    root.settings = Object.assign({}, root.settings, { showPercentage: !root.showPercentage })
    if (root.bar && root.bar.shell) root.bar.shell.updateEntryInline(root.moduleName, root.settings)
  }

  IpcHandler {
    target: "omarchy.power"

    function open() { root.open() }
    function close() { root.close() }
    function show() { root.open() }
    function hide() { root.close() }
    function toggle() { root.toggle() }
    function togglePercentage() { root.togglePercentage() }
  }

  onOpenedChanged: {
    if (opened) {
      if (!batteryPresent) {
        close()
        return
      }

      refresh()
      var idx = profiles.indexOf(activeProfile)
      profileIndex = idx >= 0 ? idx : 0
      cursorActive = false
    }
  }

  onBatteryPresentChanged: if (!batteryPresent) close()

  visible: batteryPresent
  implicitWidth: batteryPresent ? button.implicitWidth : 0
  implicitHeight: batteryPresent ? button.implicitHeight : 0

  Process {
    id: batteryProc
    command: ["/usr/bin/omarchy-battery-status", "--shell"]
    clearEnvironment: true
    environment: root.procEnv
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        batteryDeadline.stop()
        var raw = String(text || "")
        if (raw.length > 100000) return
        root.updateKeyValue(raw)
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var err = String(text || "").trim()
        if (err)
          console.warn("omarchy-battery-status stderr: " + err.substring(0, 500))
      }
    }
    onExited: batteryDeadline.stop()
  }

  Process {
    id: profilesProc
    command: ["/usr/bin/omarchy-powerprofiles-list", "--active-state"]
    clearEnvironment: true
    environment: root.procEnv
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        profilesDeadline.stop()
        var raw = String(text || "")
        if (raw.length > 100000) return
        root.updateProfiles(raw)
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var err = String(text || "").trim()
        if (err)
          console.warn("omarchy-powerprofiles-list stderr: " + err.substring(0, 500))
      }
    }
    onExited: profilesDeadline.stop()
  }

  Process {
    id: powerDataProc
    command: [root.py, root.pluginRoot + "/battery_helper.py"]
    clearEnvironment: true
    environment: root.procEnv
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        powerDataDeadline.stop()
        var raw = String(text || "")
        if (raw.length > 200000) return
        try {
          root.powerData = JSON.parse(raw)
        } catch(e) {}
      }
    }
    // Helper tracebacks land here — collect so a helper crash is visible
    // in the journal instead of looking like an empty stats payload.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var err = String(text || "").trim()
        if (err)
          console.warn("battery_helper stderr: " + err.substring(0, 500))
      }
    }
    onExited: powerDataDeadline.stop()
  }

  Process {
    id: actionProc
    clearEnvironment: true
    environment: root.procEnv
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var err = String(text || "").trim()
        if (err)
          console.warn("omarchy-powerprofiles-set stderr: " + err.substring(0, 500))
      }
    }
    onExited: { actionDeadline.stop(); root.refresh() }
  }

  // History must accrue whether or not the panel is open, or the graph only
  // ever samples the moments someone looks at it.
  Process {
    id: samplerProc
    command: [root.py, root.pluginRoot + "/battery_helper.py", "--sample"]
    clearEnvironment: true
    environment: root.procEnv
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var err = String(text || "").trim()
        if (err)
          console.warn("battery_helper --sample stderr: " + err.substring(0, 500))
      }
    }
    onExited: samplerDeadline.stop()
  }

  // Hard whole-job deadlines: a hung helper is killed and reaped rather than
  // left running indefinitely.
  Timer { id: batteryDeadline; interval: 15000; onTriggered: if (batteryProc.running) batteryProc.signal(9) }
  Timer { id: profilesDeadline; interval: 15000; onTriggered: if (profilesProc.running) profilesProc.signal(9) }
  Timer { id: powerDataDeadline; interval: 20000; onTriggered: if (powerDataProc.running) powerDataProc.signal(9) }
  Timer { id: actionDeadline; interval: 15000; onTriggered: if (actionProc.running) actionProc.signal(9) }
  Timer { id: samplerDeadline; interval: 20000; onTriggered: if (samplerProc.running) samplerProc.signal(9) }

  Timer {
    interval: 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!samplerProc.running) { samplerProc.running = true; samplerDeadline.restart() }
  }

  // Telemetry every 10s: the fuel gauge's own numbers move far slower than the
  // 5s cadence, and each tick costs a Python interpreter start.
  Timer { interval: 10000; running: root.opened; repeat: true; onTriggered: root.refreshTelemetry() }

  // Profiles on their own slow tick so the picker still notices a change made
  // outside the panel.
  Timer { interval: 30000; running: root.opened; repeat: true; onTriggered: root.refreshProfiles() }

  // Rotate the status phrase while the panel is open and we're in a
  // rotating state (charging or on battery). The text swap is wrapped in a
  // fade so the changeover reads as one organism rather than a hard cut.
  Timer {
    id: phraseTimer
    interval: 2800
    running: root.opened && root.rotatingPhrases
    repeat: true
    triggeredOnStart: false
    onTriggered: phraseSwap.restart()
  }

  SequentialAnimation {
    id: phraseSwap
    PropertyAnimation {
      target: heroStatus; property: "opacity"
      to: 0.0; duration: 180; easing.type: Easing.OutQuad
    }
    ScriptAction {
      script: {
        var n = root.activePhrases.length
        if (n > 0) root.phraseIndex = (root.phraseIndex + 1) % n
      }
    }
    PropertyAnimation {
      target: heroStatus; property: "opacity"
      to: 1.0; duration: 260; easing.type: Easing.InQuad
    }
  }

  // If we leave a rotating state mid-swap, halt the animation and snap back
  // to full opacity so "FULLY CHARGED" is legible immediately rather than
  // appearing dimmed.
  Connections {
    target: root
    function onRotatingPhrasesChanged() {
      if (!root.rotatingPhrases) {
        phraseSwap.stop()
        heroStatus.opacity = 1.0
      }
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.showPercentage && !vertical
      ? Math.round(root.batteryFraction * 100) + "% " + root.batteryIcon()
      : root.batteryIcon()
    slotSize: Style.bar.iconSlot * (root.showPercentage && !vertical ? 2 : 1)
    tooltipText: ""
    onPressed: function(b) {
      if (!root.batteryPresent) return
      if (b === Qt.RightButton) root.togglePercentage()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened && root.batteryPresent
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dx !== 0) root.selectProfileByDelta(dx)
        else if (dy !== 0) root.selectProfileByDelta(dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateSelectedProfile()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      // The card stack can exceed the panel cap — scroll instead of clipping
      // the profile picker or consumers list below the fold.
      Flickable {
        id: scrollView
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

      Column {
        id: column
        width: scrollView.width
        spacing: Style.space(14)

        // ---------- Battery card ----------
        Rectangle {
          width: parent.width
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          border.color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.08)
          border.width: 1
          implicitHeight: batteryCard.implicitHeight + Style.space(24)

          Column {
            id: batteryCard
            x: Style.space(12)
            y: Style.space(12)
            width: parent.width - Style.space(24)
            spacing: Style.space(14)

        // ---------- Hero: battery icon · title/status · percentage ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroPercent.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: root.batteryIcon()
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter

            Behavior on color { ColorAnimation { duration: 200 } }
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroPercent.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: "Battery"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              id: heroStatus
              textFormat: Text.PlainText
              text: root.heroStatusText.toUpperCase()
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Text {
            id: heroPercent
            textFormat: Text.PlainText
            text: root.batteryInfo.percentage || "—"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter

            Behavior on color { ColorAnimation { duration: 200 } }
          }
        }

        // ---------- Battery progress bar ----------
        Item {
          width: parent.width
          implicitHeight: Style.space(8)

          Rectangle {
            id: barTrack
            anchors.fill: parent
            radius: height / 2
            color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.12)
          }

          Rectangle {
            id: barFill
            anchors.left: barTrack.left
            anchors.verticalCenter: barTrack.verticalCenter
            height: barTrack.height
            radius: barTrack.radius
            color: root.batteryFillColor
            width: Math.max(barTrack.height, barTrack.width * root.batteryFraction)

            Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
            Behavior on color { ColorAnimation { duration: 220 } }

            // Subtle pulse while charging — visible signal that energy is flowing in.
            SequentialAnimation on opacity {
              running: root.charging && !root.fullyCharged && root.opened
              loops: Animation.Infinite
              alwaysRunToEnd: true
              NumberAnimation { from: 1.0; to: 0.55; duration: 950; easing.type: Easing.InOutSine }
              NumberAnimation { from: 0.55; to: 1.0; duration: 950; easing.type: Easing.InOutSine }
            }
          }
        }

        // ---------- Battery ----------
        // Visibility is intentionally only gated by "we've ever loaded data" so
        // the section never collapses mid-transition. fullyCharged is *not* part
        // of the condition: UPower briefly reports FullyCharged on plug-in when
        // the battery sits above the charge-control start threshold, and we
        // refuse to flicker the whole panel for that ~1s window.
        //
        // Six numbers across two rows: the four worth acting on, plus the
        // negotiated adapter ceiling (a weak PSU reads plainly) and the
        // hottest thermal sensor (the charge regulator sprints during
        // fast-charge). Voltage/current/coulombs stay in the JSON — a fourth
        // row would turn a glanceable panel into a spreadsheet.
        Column {
          visible: root.batteryInfo.percentage !== undefined
          width: parent.width
          spacing: Style.spacing.labelGap

          Row {
            width: parent.width
            spacing: Style.space(20)

            Column {
              width: (parent.width - parent.spacing) / 2
              InfoPair {
                label: root.discharging ? "Time left" : "Time to full"
                value: root.batteryFlowIdle && !root.haveTele ? "-" : root.timeText()
              }
            }

            Column {
              width: (parent.width - parent.spacing) / 2
              InfoPair { label: "Power"; value: root.powerText() }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(20)

            Column {
              width: (parent.width - parent.spacing) / 2
              InfoPair { label: "Energy"; value: root.energyText() }
            }

            Column {
              width: (parent.width - parent.spacing) / 2
              InfoPair {
                label: "AC adapter"
                value: Model.fmtAdapter(root.tele.power_w, root.tele.adapter_limit_w)
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(20)

            Column {
              width: (parent.width - parent.spacing) / 2
              InfoPair {
                label: "Thermals"
                value: Model.fmtThermal(root.tele.temp_c, root.tele.temps_c)
              }
            }

            Column {
              width: (parent.width - parent.spacing) / 2
              InfoPair {
                label: "Pack volts"
                value: {
                  var v = root.tele.voltage_v
                  return (typeof v === "number") ? v.toFixed(2) + " V" : Model.DASH
                }
              }
            }
          }
        }

        // ---------- Battery health ----------
        // The one number a battery widget should lead with after charge level:
        // how much of the pack is left for good. It gets a bar rather than a
        // label row because it's a slowly-moving quantity you want to feel,
        // not read. Colour stays neutral until it actually degrades.
        HealthGauge {
          visible: root.haveTele
          fraction: root.healthFraction
          headline: root.healthHeadline
          caption: root.healthCaption
          degraded: root.healthDegraded
        }

        // Everything else is real but rarely actionable, so it collapses to one
        // quiet line instead of three more label rows.
        Text {
          width: parent.width
          visible: root.haveTele
          textFormat: Text.PlainText
          // packFooter is a readonly property, not a function — calling it
          // here silently yields an empty string and the line vanishes.
          text: root.packFooter
          color: root.bar.foreground
          opacity: 0.45
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          // Wrap rather than elide: a long gauge model must push the line to a
          // second row, not silently amputate the last value.
          wrapMode: Text.WordWrap
          // Left-aligned with everything else above it. Centred, this line was
          // the only ragged-left row in the panel, so the eye landed on the
          // quietest line on the screen — backwards for a footnote.
          horizontalAlignment: Text.AlignLeft
        }
          }
        }

        // ---------- Charge history card ----------
        Rectangle {
          width: parent.width
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          border.color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.08)
          border.width: 1
          visible: !!root.powerData.ascii_graph
          implicitHeight: historyCard.implicitHeight + Style.space(24)

          Column {
            id: historyCard
            x: Style.space(12)
            y: Style.space(12)
            width: parent.width - Style.space(24)
            spacing: Style.space(6)

          PanelSectionHeader {
            text: "BATTERY CHARGE HISTORY"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          Rectangle {
            width: parent.width
            implicitHeight: asciiText.implicitHeight + Style.space(12)
            radius: Style.space(6)
            color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.06)

            Text {
              id: asciiText
              anchors.centerIn: parent
              textFormat: Text.PlainText
              text: root.powerData.ascii_graph || ""
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              lineHeight: 1.15
            }
          }
          // Watts over the same window — the charge line shows where the
          // level went, this shows the flow that moved it. Magnitude only;
          // sign lives in the "Power" readout above.
          Text {
            width: parent.width
            visible: !!root.powerData.watts_graph
            textFormat: Text.PlainText
            text: root.powerData.watts_graph || ""
            color: Style.selectedFillFor(root.bar.foreground, Color.accent)
            opacity: 0.8
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            visible: !!root.powerData.watts_graph
            textFormat: Text.PlainText
            text: "watts"
            color: root.bar.foreground
            opacity: 0.4
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignRight
          }
          // The caption under the sparkline already carries the least-squares
          // drain slope, so a separate "Drain rate" row would only repeat it.
          }
        }

        // ---------- Drawing power now card ----------
        Rectangle {
          width: parent.width
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          border.color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.08)
          border.width: 1
          visible: !!root.powerData.top_consumers && root.powerData.top_consumers.length > 0
          implicitHeight: consumersCard.implicitHeight + Style.space(24)

          Column {
            id: consumersCard
            x: Style.space(12)
            y: Style.space(12)
            width: parent.width - Style.space(24)
            spacing: Style.space(6)

          PanelSectionHeader {
            text: "DRAWING POWER NOW"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          // Rates are measured over the refresh interval, not lifetime %CPU —
          // the column that used to rank by process age. Watts are each
          // process's share of busy cores applied to the pack's current draw:
          // honest as an estimate, which is why they carry the "≈" prefix.
          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "cpu share over ~30 s · ≈W of pack draw"
            color: root.bar.foreground
            opacity: 0.4
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Column {
            width: parent.width
            spacing: Style.space(4)

            Repeater {
              model: root.powerData.top_consumers || []

              Row {
                required property var modelData
                width: parent.width
                spacing: Style.space(8)

                Text {
                  width: Style.space(88)
                  textFormat: Text.PlainText
                  text: modelData.name
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                  elide: Text.ElideRight
                }

                Text {
                  width: Style.space(52)
                  textFormat: Text.PlainText
                  text: modelData.cpu
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  horizontalAlignment: Text.AlignRight
                }

                Text {
                  width: Style.space(56)
                  textFormat: Text.PlainText
                  text: Model.fmtEstWatts(modelData.watts)
                  color: root.bar.foreground
                  opacity: 0.7
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  horizontalAlignment: Text.AlignRight
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: modelData.bar
                  color: Style.selectedFillFor(root.bar.foreground, Color.accent)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }
        }

        // ---------- Power profile card ----------
        Rectangle {
          width: parent.width
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          border.color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.08)
          border.width: 1
          implicitHeight: profileCard.implicitHeight + Style.space(24)

          Column {
            id: profileCard
            x: Style.space(12)
            y: Style.space(12)
            width: parent.width - Style.space(24)
            spacing: Style.space(10)

          PanelSectionHeader {
            text: "POWER PROFILE"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          Row {
            id: profileRow
            width: parent.width
            spacing: Style.space(6)

            readonly property real cellWidth: root.profiles.length > 0
              ? (width - spacing * (root.profiles.length - 1)) / root.profiles.length
              : 0

            Repeater {
              model: root.profiles
              Button {
                required property var modelData
                required property int index
                width: profileRow.cellWidth
                iconText: root.profileIcon(String(modelData))
                iconSize: Style.font.title
                text: String(modelData).charAt(0).toUpperCase() + String(modelData).slice(1)
                fontSize: Style.font.bodySmall
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                horizontalPadding: Style.spacing.controlPaddingX
                verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
                bordered: true
                active: root.activeProfile === modelData
                hasCursor: root.cursorActive && root.profileIndex === index
                onClicked: root.setProfile(modelData)
                onHovered: function(h) {
                  if (h) {
                    root.cursorActive = true
                    root.profileIndex = index
                  }
                }
              }
            }
          }
          }
        }
      }
      }
    }
  }

  // Long-term capacity as a quiet bar. Neutral until the pack is genuinely
  // worn, so a healthy battery adds no colour noise to the bar at all.
  component HealthGauge: Column {
    id: gauge
    property real fraction: 0
    property string headline: ""
    property string caption: ""
    property bool degraded: false

    width: gauge.parent ? gauge.parent.width : 0
    spacing: Style.space(5)

    Item {
      width: parent.width
      implicitHeight: gaugeText.implicitHeight

      Text {
        id: gaugeLabel
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: "BATTERY HEALTH"
        color: root.bar.foreground
        opacity: 0.6
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        font.letterSpacing: 1.2
      }

      Text {
        id: gaugeText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: gauge.headline
        color: gauge.degraded ? Color.urgent : root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true

        Behavior on color { ColorAnimation { duration: 260 } }
      }
    }

    Rectangle {
      width: parent.width
      height: Style.space(3)
      radius: height / 2
      color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.12)

      Rectangle {
        id: gaugeFill
        height: parent.height
        radius: parent.radius
        color: gauge.degraded ? Color.urgent : root.bar.foreground
        opacity: 0.85
        width: Math.max(0, Math.min(1, gauge.fraction)) * parent.width

        Behavior on width { NumberAnimation { duration: 480; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: 260 } }
      }
    }

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: gauge.caption
      color: root.bar.foreground
      opacity: 0.45
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }

  component InfoPair: Item {
    property string label: ""
    property string value: ""

    width: parent.width
    implicitHeight: Math.max(infoLabel.implicitHeight, infoValue.implicitHeight)

    InfoLabel {
      id: infoLabel
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: label
    }

    // Anchored between label and right edge with elide: the spacer-Item Row
    // this replaced let a long value draw clean past its column.
    InfoValue {
      id: infoValue
      anchors.left: infoLabel.right
      anchors.leftMargin: Style.space(8)
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: value
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideRight
    }
  }

  component InfoLabel: Text {
    textFormat: Text.PlainText
    color: root.bar.foreground
    opacity: 0.6
    font.family: root.bar.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  component InfoValue: Text {
    textFormat: Text.PlainText
    color: root.bar.foreground
    font.family: root.bar.fontFamily
    font.pixelSize: Style.font.bodySmall
  }
}
