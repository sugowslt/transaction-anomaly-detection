package com.sugowslt.fraudlab

import java.math.BigDecimal
import kotlin.math.ln1p
import kotlin.math.sqrt

/** Independent ledger observations; includes self and peers only after the bucket closes. */
object BankWindowObservations {
    val fields = listOf("window_sender_count_log", "window_sender_amount_log", "window_sender_mean_log", "window_sender_max_log",
        "window_sender_min_log", "window_sender_std_log", "window_sender_amount_cv", "window_amount_sender_total_share",
        "window_sender_distinct_recipients_log", "window_sender_distinct_amounts_log", "window_sender_same_amount_share", "window_pair_count_log",
        "window_pair_amount_log", "window_sender_pair_share", "window_recipient_count_log", "window_recipient_amount_log", "window_recipient_mean_log",
        "window_recipient_std_log", "window_recipient_distinct_senders_log", "window_reciprocal_count_log")
    private data class Account(val institution: String, val account: String)
    private data class PairKey(val source: Account, val destination: Account)
    private data class Stats(val count: Int, val total: Double, val mean: Double, val min: Double, val max: Double, val std: Double)
    private fun source(t: BankContextualTransaction) = Account(t.sourceInstitution, t.sourceAccount)
    private fun destination(t: BankContextualTransaction) = Account(t.destinationInstitution, t.destinationAccount)
    private fun stats(rows: List<BankContextualTransaction>): Stats {
        val amounts = rows.map { it.amount }.sorted()
        val total = amounts.fold(BigDecimal.ZERO, BigDecimal::add).toDouble()
        val mean = total / amounts.size
        val std = sqrt(amounts.sumOf { val diff = it.toDouble() - mean; diff * diff } / amounts.size)
        return Stats(amounts.size, total, mean, amounts.first().toDouble(), amounts.last().toDouble(), std)
    }

    fun calculate(rows: List<BankContextualTransaction>): List<Map<String, Double>> {
        require(rows.size in 1..20000) { "A complete window requires 1-20000 rows" }
        require(rows.map { it.transactionDate to it.timeBucket }.distinct().size == 1) { "Mixed observation windows" }
        val senders = rows.groupBy(::source)
        val recipients = rows.groupBy(::destination)
        val pairs = rows.groupBy { PairKey(source(it), destination(it)) }
        val senderStats = senders.mapValues { stats(it.value) }
        val recipientStats = recipients.mapValues { stats(it.value) }
        val pairStats = pairs.mapValues { stats(it.value) }
        val senderParties = senders.mapValues { it.value.map(::destination).distinct().size }
        val recipientParties = recipients.mapValues { it.value.map(::source).distinct().size }
        val amountCounts = senders.mapValues { it.value.groupingBy { row -> row.amount.stripTrailingZeros() }.eachCount() }
        return rows.map { row ->
            val source = source(row)
            val destination = destination(row)
            val s = senderStats.getValue(source)
            val r = recipientStats.getValue(destination)
            val p = pairStats.getValue(PairKey(source, destination))
            val reverseCount = pairStats[PairKey(destination, source)]?.count ?: 0
            val amounts = amountCounts.getValue(source)
            val values = listOf(ln1p(s.count.toDouble()), ln1p(s.total), ln1p(s.mean), ln1p(s.max), ln1p(s.min), ln1p(s.std),
                if (s.mean > 0) (s.std / s.mean).coerceAtMost(100.0) else 0.0,
                if (s.total > 0) row.amount.toDouble() / s.total else 0.0,
                ln1p(senderParties.getValue(source).toDouble()), ln1p(amounts.size.toDouble()),
                amounts.getValue(row.amount.stripTrailingZeros()).toDouble() / s.count,
                ln1p(p.count.toDouble()), ln1p(p.total), p.count.toDouble() / s.count,
                ln1p(r.count.toDouble()), ln1p(r.total), ln1p(r.mean), ln1p(r.std),
                ln1p(recipientParties.getValue(destination).toDouble()), ln1p(reverseCount.toDouble()))
            require(values.all { it.isFinite() && it >= 0 }) { "Invalid ledger observations" }
            fields.zip(values).toMap()
        }
    }
}
