package com.sugowslt.fraudlab

import kotlin.math.abs
import tools.jackson.databind.JsonNode

data class BankScoreGroup(
    val group: String,
    val label: String,
    val contribution: Double,
    val features: List<String>,
)

data class BankModelExplanation(
    val method: String,
    val outputUnit: String,
    val baselineScore: Double,
    val explainedScore: Double,
    val reconstructionError: Double,
    val coalitionsEvaluated: Int,
    val normalReferenceRows: Long,
    val groups: List<BankScoreGroup>,
    val interpretation: String,
) {
    companion object {
        /** Stored rows have already passed protocol validation before commit. */
        fun fromJson(node: JsonNode): BankModelExplanation = BankModelExplanation(
            node["method"].stringValue(), node["outputUnit"].stringValue(),
            node["baselineScore"].doubleValue(), node["explainedScore"].doubleValue(), node["reconstructionError"].doubleValue(),
            node["coalitionsEvaluated"].intValue(), node["normalReferenceRows"].longValue(),
            node["groups"].values().map { group ->
                BankScoreGroup(group["group"].stringValue(), group["label"].stringValue(), group["contribution"].doubleValue(),
                    group["features"].values().map { it.stringValue() })
            },
            node["interpretation"].stringValue(),
        )
    }
}

/** The explanation is model-score attribution, never a causal fraud finding. */
object BankModelExplanationProtocol {
    const val METHOD = "exact-group-shapley-score-v1"
    const val INTERPRETATION = "Relative to normal-fit mean/mode reference; depends on grouping and correlated features; not causal or proof of fraud"
    val GROUP_LABELS = linkedMapOf(
        "amount" to "현재·과거 금액과 금액 대비",
        "transaction_conditions" to "시간대·이체 방식·자금 종류",
        "sender_patterns" to "출금 빈도와 수신처 반복",
        "elapsed_history" to "이전 거래 이후 경과 기간",
        "intraday_outflow" to "당일·최근 24시간 출금 패턴",
        "sender_inflow" to "출금계좌의 과거 입금 흐름",
        "recipient_flow" to "수신계좌의 과거 입출금 흐름",
    )

    fun featureGroup(name: String): String? {
        val lower = name.lowercase()
        return when {
            "amount" in lower || "net_" in lower -> "amount"
            lower.startsWith("days_since") -> "elapsed_history"
            lower.startsWith("graph_recipient") -> "recipient_flow"
            lower.startsWith("graph_sender") -> if ("inflow" in lower) "sender_inflow" else "intraday_outflow"
            lower in setOf("time_bucket", "fund_code", "channel_code", "same_institution", "day_of_week") ||
                listOf("same_channel", "same_fund", "same_time").any { it in lower } -> "transaction_conditions"
            lower.startsWith("sender_") || lower.startsWith("recipient_") -> "sender_patterns"
            else -> null
        }
    }

    fun validate(node: JsonNode?, riskScore: Double, features: Set<String>): BankModelExplanation? {
        if (!riskScore.isFinite() || riskScore !in 0.0..1.0 || features.size != 66) return null
        if (node?.isObject != true || node.properties().map { it.key }.toSet() != setOf(
                "method", "outputUnit", "baselineScore", "explainedScore", "reconstructionError", "coalitionsEvaluated", "normalReferenceRows", "groups", "interpretation")) return null
        if (node["method"]?.isString != true || node["method"].stringValue() != METHOD ||
            node["outputUnit"]?.isString != true || node["outputUnit"].stringValue() != "model_score_0_to_1" ||
            node["interpretation"]?.isString != true || node["interpretation"].stringValue() != INTERPRETATION ||
            node["coalitionsEvaluated"]?.isIntegralNumber != true || !node["coalitionsEvaluated"].canConvertToInt() || node["coalitionsEvaluated"].intValue() != 128 ||
            node["normalReferenceRows"]?.isIntegralNumber != true || !node["normalReferenceRows"].canConvertToLong() || node["normalReferenceRows"].longValue() <= 0 ||
            node["groups"]?.isArray != true || node["groups"].size() != 7) return null
        for (name in listOf("baselineScore", "explainedScore", "reconstructionError")) {
            if (node[name]?.isNumber != true || !node[name].doubleValue().isFinite()) return null
        }
        val baseline = node["baselineScore"].doubleValue()
        val explained = node["explainedScore"].doubleValue()
        val reportedError = node["reconstructionError"].doubleValue()
        if (baseline !in 0.0..1.0 || explained !in 0.0..1.0 || reportedError !in 0.0..1e-8 || abs(explained - riskScore) > 1e-8) return null
        val usedGroups = mutableSetOf<String>()
        val usedFeatures = mutableListOf<String>()
        var contributions = 0.0
        for (group in node["groups"].values()) {
            if (!group.isObject || group.properties().map { it.key }.toSet() != setOf("group", "label", "contribution", "features") ||
                group["group"]?.isString != true || group["label"]?.isString != true ||
                group["contribution"]?.isNumber != true || !group["contribution"].doubleValue().isFinite() ||
                group["contribution"].doubleValue() !in (-1.0 - 1e-8)..(1.0 + 1e-8) || group["features"]?.isArray != true || group["features"].isEmpty) return null
            val key = group["group"].stringValue()
            if (key !in GROUP_LABELS || !usedGroups.add(key) || group["label"].stringValue() != GROUP_LABELS[key]) return null
            val entries = group["features"].values().toList()
            if (entries.any { !it.isString }) return null
            val names = entries.map { it.stringValue() }
            if (names.size != names.toSet().size || names.toSet() != features.filter { featureGroup(it) == key }.toSet()) return null
            usedFeatures += names
            contributions += group["contribution"].doubleValue()
        }
        if (usedGroups != GROUP_LABELS.keys || usedFeatures.size != features.size || usedFeatures.toSet() != features) return null
        val reconstruction = baseline + contributions
        if (abs(reconstruction - riskScore) > 1e-8 || abs(abs(reconstruction - explained) - reportedError) > 1e-8) return null
        return BankModelExplanation.fromJson(node)
    }
}
