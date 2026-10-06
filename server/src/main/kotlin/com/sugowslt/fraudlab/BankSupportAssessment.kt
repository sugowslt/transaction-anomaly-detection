package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.time.LocalDate
import tools.jackson.databind.JsonNode

data class BankSupportAssessment(
    val status: String,
    val reasons: List<String>,
    val fitAmountMin: Double,
    val fitAmountMax: Double,
    val validationYear: Int,
) {
    companion object {
        fun expected(transaction: BankContextualTransaction, contextStatus: String): BankSupportAssessment {
            return assess(transaction.amount, transaction.transactionDate, contextStatus)
        }

        fun assess(amount: BigDecimal, date: LocalDate, contextStatus: String, researchCandidate: Boolean = true): BankSupportAssessment {
            val outsideAmount = amount < BigDecimal.ONE || amount > BigDecimal("500000000")
            val cold = contextStatus == "cold_start"
            val outsideDate = date.year != 2024
            val status = when {
                outsideAmount -> "outside_fit_support"
                cold -> "insufficient_context"
                outsideDate -> "outside_temporal_validation"
                else -> "within_observed_support"
            }
            val reasons = mutableListOf("synthetic_labels_only", "local_history_completeness_unverified")
            if (researchCandidate) reasons += "research_candidate"
            if (outsideAmount) reasons += "amount_outside_fit_range"
            if (cold) reasons += "cold_context"
            if (outsideDate) reasons += "date_outside_retrospective_evaluation"
            return BankSupportAssessment(status, reasons, 1.0, 500000000.0, 2024)
        }

        fun validate(node: JsonNode?, transaction: BankContextualTransaction, contextStatus: String): BankSupportAssessment? {
            if (node?.isObject != true || node.properties().map { it.key }.toSet() != setOf("status", "reasons", "fitAmountMin", "fitAmountMax", "validationYear") ||
                node["status"]?.isString != true || node["reasons"]?.isArray != true || node["reasons"].values().any { !it.isString } ||
                node["fitAmountMin"]?.isNumber != true || node["fitAmountMax"]?.isNumber != true ||
                !node["fitAmountMin"].doubleValue().isFinite() || !node["fitAmountMax"].doubleValue().isFinite() ||
                node["fitAmountMin"].decimalValue().compareTo(BigDecimal.ONE) != 0 ||
                node["fitAmountMax"].decimalValue().compareTo(BigDecimal("500000000")) != 0 ||
                node["validationYear"]?.isIntegralNumber != true || !node["validationYear"].canConvertToInt() || node["validationYear"].intValue() != 2024) return null
            val expected = expected(transaction, contextStatus)
            if (node["status"].stringValue() != expected.status || node["reasons"].values().map { it.stringValue() }.toList() != expected.reasons) return null
            return expected
        }

        fun fromJson(node: JsonNode) = BankSupportAssessment(node["status"].stringValue(), node["reasons"].values().map { it.stringValue() },
            node["fitAmountMin"].doubleValue(), node["fitAmountMax"].doubleValue(), node["validationYear"].intValue())
    }
}
