import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// One chip for the local open-source cluster: tokens served, plus a dot for the
// engine. The widget only draws what cluster_tokens.py prints once a minute.
BarWidget {
  id: root
  moduleName: "meshvault.cluster-tokens"

  readonly property string script: Qt.resolvedUrl("cluster_tokens.py").toString().replace(/^file:\/\//, "")
  readonly property var periodKeys: ["today", "7d", "30d", "all"]
  readonly property var periodNames: ({ "today": "today", "7d": "7 days", "30d": "30 days", "all": "all time" })
  readonly property int staleAfterMs: 300000
  readonly property color upColor: "#8fd0b0"

  property var report: null
  property double reportAt: 0
  property bool failed: false
  property int periodIndex: 0
  property double nowMs: Date.now()

  readonly property string periodKey: periodKeys[periodIndex]
  readonly property bool stale: failed || report === null || (nowMs - reportAt) > staleAfterMs
  readonly property bool engineUp: report !== null && report.engine !== undefined && report.engine.up === true

  function fmt(value) {
    var n = Number(value) || 0
    var out
    if (n >= 1e9) out = (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B"
    else if (n >= 1e6) out = (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M"
    else if (n >= 1e3) out = (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K"
    else out = String(Math.round(n))
    return out.replace(/\.0(?=[KMB]$)/, "")
  }

  function bucket(period) {
    return report && report.periods && report.periods[period] ? report.periods[period] : null
  }

  function chipText() {
    var t = bucket(periodKey)
    return t ? fmt(t.total) : "…"
  }

  function duration(seconds) {
    var s = Number(seconds) || 0
    if (s >= 86400) return Math.floor(s / 86400) + " d"
    if (s >= 3600) return Math.floor(s / 3600) + " h"
    return Math.max(1, Math.floor(s / 60)) + " min"
  }

  function sinceText() {
    if (!report || !report.since) return ""
    var d = new Date(Number(report.since) * 1000)
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    var h = d.getHours()
    var m = d.getMinutes()
    return months[d.getMonth()] + " " + d.getDate() + " " + (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m
  }

  function requestsText(period, t) {
    if (t.requests === null || t.requests === undefined) return "requests n/a"
    var partial = report && report.requests_partial && report.requests_partial.indexOf(period) >= 0
    return (partial ? "at least " : "") + t.requests + " requests"
  }

  function buildTooltip() {
    if (!report) return failed ? "Cluster tokens: no data yet. Right-click to retry." : "Cluster tokens: loading"
    var eng = report.engine || {}
    var lines = ["Open-source cluster" + (eng.model ? " · " + eng.model : "")]
    if (eng.up) {
      var live = []
      if (eng.uptime_s) live.push("up " + duration(eng.uptime_s))
      if (eng.running !== null && eng.running !== undefined) live.push(eng.running + " running, " + (eng.waiting || 0) + " waiting")
      if (eng.kv_usage !== null && eng.kv_usage !== undefined) live.push("KV " + Math.round(Number(eng.kv_usage) * 100) + "%")
      if (eng.decode_tps) live.push(eng.decode_tps + " tok/s per request")
      lines.push("Engine up" + (live.length ? " · " + live.join(" · ") : ""))
    } else {
      lines.push("Engine down" + (eng.error ? " (" + eng.error + ")" : eng.stale ? " (no recent sample)" : ""))
    }
    lines.push("")
    lines.push("Tokens served (prompt + generated)")
    for (var i = 0; i < periodKeys.length; i++) {
      var k = periodKeys[i]
      var t = bucket(k)
      if (!t) continue
      lines.push("  " + periodNames[k] + ": " + fmt(t.total) + "  (prompt " + fmt(t.prompt) + ", generated " + fmt(t.generation)
        + ", " + requestsText(k, t) + ")")
    }
    if (report.since) lines.push("Counted since " + sinceText() + ". Earlier traffic was not recorded.")
    if (report.errors && report.errors.length > 0) lines.push("Read problem: " + report.errors[0].message)
    if (stale) lines.push("Data is old or the last read failed")
    lines.push("Click: switch period (now " + periodNames[periodKey] + ") · Right-click: refresh")
    return lines.join("\n")
  }

  readonly property string tooltip: buildTooltip()

  function stateJson() {
    return JSON.stringify({ "period": periodKey, "stale": stale, "engineUp": engineUp, "text": chipText(), "tooltip": tooltip })
  }

  function ingest(line) {
    var raw = String(line || "").trim()
    if (raw === "") return
    try {
      var data = JSON.parse(raw)
      if (data && data.version === 1 && data.periods) {
        root.report = data
        root.reportAt = Date.now()
        root.failed = false
      } else {
        root.failed = true
      }
    } catch (e) {
      root.failed = true
    }
  }

  function refresh() {
    if (probe.running) return
    probe.running = true
  }

  function cyclePeriod() {
    root.periodIndex = (root.periodIndex + 1) % root.periodKeys.length
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: probe
    command: ["python3", root.script]
    stdout: SplitParser {
      onRead: function(line) { root.ingest(line) }
    }
    onRunningChanged: if (!running) root.nowMs = Date.now()
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      root.nowMs = Date.now()
      root.refresh()
    }
  }

  IpcHandler {
    target: "meshvault.cluster-tokens"

    function refresh(): void { root.refresh() }
    function cycle(): void { root.cyclePeriod() }
    function state(): string { return root.stateJson() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    fixedWidth: chip.implicitWidth + Style.space(17)
    tooltipText: root.tooltip

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton || mouseButton === Qt.MiddleButton) root.refresh()
      else root.cyclePeriod()
    }

    Row {
      id: chip
      anchors.centerIn: parent
      spacing: Style.space(4)
      opacity: root.stale ? 0.55 : 1

      Rectangle {
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(5)
        height: width
        radius: width / 2
        color: root.engineUp ? root.upColor : button.activeColor
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: "OSS"
        color: Qt.rgba(button.foreground.r, button.foreground.g, button.foreground.b, 0.6)
        font.family: button.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: root.chipText()
        color: button.foreground
        font.family: button.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        visible: root.periodKey !== "today"
        text: root.periodKey
        color: Qt.rgba(button.foreground.r, button.foreground.g, button.foreground.b, 0.6)
        font.family: button.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        visible: root.stale && root.report !== null
        text: "!"
        color: button.activeColor
        font.family: button.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
    }
  }
}
