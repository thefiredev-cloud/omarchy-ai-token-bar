import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Three chips: tokens used with Anthropic, Codex and Grok. The widget only draws
// what ai_tokens.py prints once a minute; the script owns the ledger.
BarWidget {
  id: root
  moduleName: "meshvault.ai-tokens"

  readonly property string script: Qt.resolvedUrl("ai_tokens.py").toString().replace(/^file:\/\//, "")
  readonly property var providerKeys: ["anthropic", "codex", "grok"]
  readonly property var shortNames: ({ "anthropic": "CLD", "codex": "CDX", "grok": "GRK" })
  readonly property var longNames: ({ "anthropic": "Anthropic", "codex": "Codex", "grok": "Grok" })
  readonly property var periodKeys: ["today", "7d", "30d", "all"]
  readonly property var periodNames: ({ "today": "today", "7d": "7 days", "30d": "30 days", "all": "all time" })
  readonly property var sourceNames: ({ "claude_code": "Claude Code", "codex_cli": "Codex CLI", "grok_cli": "Grok CLI", "omp": "OMP", "hermes": "Hermes" })
  readonly property int staleAfterMs: 300000

  property var report: null
  property double reportAt: 0
  property bool failed: false
  property int periodIndex: 0
  property double nowMs: Date.now()

  readonly property string periodKey: periodKeys[periodIndex]
  readonly property bool stale: failed || report === null || (nowMs - reportAt) > staleAfterMs
  readonly property bool counting: report !== null && report.backfill !== undefined && report.backfill.complete === false

  function fmt(value) {
    var n = Number(value) || 0
    var out
    if (n >= 1e9) out = (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B"
    else if (n >= 1e6) out = (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M"
    else if (n >= 1e3) out = (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K"
    else out = String(Math.round(n))
    return out.replace(/\.0(?=[KMB]$)/, "")
  }

  function bucket(provider, period) {
    var p = report && report.providers ? report.providers[provider] : null
    var t = p && p.periods ? p.periods[period] : null
    return t ? t : null
  }

  function totalFor(provider, period) {
    var t = bucket(provider, period)
    return t ? Number(t.total) || 0 : 0
  }

  function chipText(provider) {
    return report ? fmt(totalFor(provider, periodKey)) : "…"
  }

  function sourceLine(provider) {
    var p = report && report.providers ? report.providers[provider] : null
    if (!p || !p.sources) return ""
    var parts = []
    for (var name in p.sources) {
      var t = p.sources[name][periodKey]
      var total = t ? Number(t.total) || 0 : 0
      if (total > 0) parts.push((sourceNames[name] || name) + " " + fmt(total))
    }
    return parts.join(" · ")
  }

  function buildTooltip() {
    if (!report) return failed ? "AI tokens: no data yet. Right-click to retry." : "AI tokens: loading"
    var lines = ["Tokens used (input + output + cache writes)"]
    for (var i = 0; i < providerKeys.length; i++) {
      var k = providerKeys[i]
      lines.push(longNames[k] + "   today " + fmt(totalFor(k, "today")) + " · 7d " + fmt(totalFor(k, "7d"))
        + " · 30d " + fmt(totalFor(k, "30d")) + " · all " + fmt(totalFor(k, "all")))
    }
    lines.push("")
    lines.push("By source, " + periodNames[periodKey] + ":")
    for (var j = 0; j < providerKeys.length; j++) {
      var key = providerKeys[j]
      var detail = sourceLine(key)
      var cache = bucket(key, periodKey)
      lines.push("  " + longNames[key] + ": " + (detail !== "" ? detail : "none")
        + (cache && Number(cache.cache_read) > 0 ? "  (cache reads " + fmt(cache.cache_read) + ", not counted)" : ""))
    }
    if (counting) lines.push("Still counting older history (" + report.backfill.pending_files + " files left)")
    if (report.errors && report.errors.length > 0) {
      var bad = []
      for (var e = 0; e < report.errors.length; e++) bad.push(report.errors[e].source)
      lines.push("Unavailable: " + bad.join(", "))
    }
    if (stale) lines.push("Data is old or the last read failed")
    lines.push("Click: switch period (now " + periodNames[periodKey] + ") · Right-click: refresh")
    return lines.join("\n")
  }

  readonly property string tooltip: buildTooltip()

  function stateJson() {
    var chips = []
    for (var i = 0; i < providerKeys.length; i++) {
      chips.push({ "short": shortNames[providerKeys[i]], "text": chipText(providerKeys[i]) })
    }
    return JSON.stringify({ "period": periodKey, "stale": stale, "counting": counting, "chips": chips, "tooltip": tooltip })
  }

  function ingest(line) {
    var raw = String(line || "").trim()
    if (raw === "") return
    try {
      var data = JSON.parse(raw)
      if (data && data.version === 1 && data.providers) {
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

  // The script is incremental, so a minute between reads is cheap. A long first
  // count continues across ticks because the script saves its progress.
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
    target: "meshvault.ai-tokens"

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
    fixedWidth: chips.implicitWidth + Style.space(17)
    tooltipText: root.tooltip

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton || mouseButton === Qt.MiddleButton) root.refresh()
      else root.cyclePeriod()
    }

    Row {
      id: chips
      anchors.centerIn: parent
      spacing: Style.space(9)
      opacity: root.stale ? 0.55 : 1

      Repeater {
        model: root.providerKeys

        delegate: Row {
          spacing: Style.space(3)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: root.shortNames[modelData]
            color: Qt.rgba(button.foreground.r, button.foreground.g, button.foreground.b, 0.6)
            font.family: button.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: root.chipText(modelData)
            color: button.foreground
            font.family: button.fontFamily
            font.pixelSize: Style.font.body
          }
        }
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
        visible: root.counting
        text: "…"
        color: button.foreground
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
