package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.sql.ResultSet
import java.time.LocalDate
import javax.sql.DataSource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.jdbc.datasource.DataSourceTransactionManager
import org.springframework.stereotype.Repository
import org.springframework.transaction.support.TransactionTemplate

/** The nine raw fields available to the contextual bank model at decision time. */
data class BankContextualTransaction(
    val sourceAccount: String,
    val destinationAccount: String,
    val sourceInstitution: String,
    val destinationInstitution: String,
    val fundType: String,
    val amount: BigDecimal,
    val timeBucket: Int,
    val channel: String,
    val transactionDate: LocalDate,
) {
    fun modelInput(): Map<String, Any> = linkedMapOf(
        "출금계좌일련번호" to sourceAccount,
        "입금계좌일련번호" to destinationAccount,
        "출금금융회사일련번호" to sourceInstitution,
        "입금금융회사일련번호" to destinationInstitution,
        "자금구분" to fundType,
        "거래금액" to amount,
        "거래시간대" to timeBucket,
        "매체구분" to channel,
        "거래일자" to transactionDate.format(java.time.format.DateTimeFormatter.BASIC_ISO_DATE),
    )
}

/** All-time, strictly pre-event aggregates; raw recent history remains separate. */
data class BankPriorSummary(
    val senderCount: Long,
    val senderAmountSum: BigDecimal,
    val senderAmountMax: BigDecimal,
    val distinctRecipients: Long,
    val recipientCount: Long,
    val recipientBankCount: Long,
    val sameChannelCount: Long,
    val sameFundCount: Long,
    val firstDate: LocalDate?,
    val lastDate: LocalDate?,
    val lastRecipientDate: LocalDate?,
) {
    fun modelInput(): Map<String, Any?> = linkedMapOf(
        "senderCount" to senderCount,
        "senderAmountSum" to senderAmountSum,
        "senderAmountMax" to senderAmountMax,
        "distinctRecipients" to distinctRecipients,
        "recipientCount" to recipientCount,
        "recipientBankCount" to recipientBankCount,
        "sameChannelCount" to sameChannelCount,
        "sameFundCount" to sameFundCount,
        "firstDate" to firstDate?.format(java.time.format.DateTimeFormatter.BASIC_ISO_DATE),
        "lastDate" to lastDate?.format(java.time.format.DateTimeFormatter.BASIC_ISO_DATE),
        "lastRecipientDate" to lastRecipientDate?.format(java.time.format.DateTimeFormatter.BASIC_ISO_DATE),
    )
}

data class BankContextualEvidence(
    val decision: BankDecision,
    val transactionDate: LocalDate,
    val historyCount: Int,
    val historyWindowStart: LocalDate,
    val historyStatus: String,
    val contextStatus: String,
    val evidenceType: String,
    val priorSummary: BankPriorSummary,
    val featureSchemaVersion: String,
    val observedContext: Map<String, Double>,
    val featureSnapshot: Map<String, Double>,
) {
    // The fixed v1 model has the same observed amount range as v2. Derive
    // this policy from immutable saved values, never a later account history.
    val supportAssessment: BankSupportAssessment
        get() = BankSupportAssessment.assess(decision.amount, transactionDate, contextStatus, researchCandidate = false)
}

@Repository
class BankContextualEventRepository(
    private val jdbc: JdbcTemplate,
    private val decisions: BankDecisionRepository,
) {
    private val transactionTemplate = TransactionTemplate(DataSourceTransactionManager(requiredDataSource(jdbc)))
    private val historyMapper = RowMapper { result: ResultSet, _: Int ->
        BankContextualTransaction(
            sourceAccount = result.getString("source_account"),
            destinationAccount = result.getString("destination_account"),
            sourceInstitution = result.getString("source_institution"),
            destinationInstitution = result.getString("destination_institution"),
            fundType = result.getString("fund_type"),
            amount = result.getBigDecimal("amount"),
            timeBucket = result.getInt("time_bucket"),
            channel = result.getString("channel"),
            transactionDate = result.getObject("transaction_date", LocalDate::class.java),
        )
    }

    fun latestDate(transaction: BankContextualTransaction): LocalDate? = jdbc.query(
        "SELECT MAX(transaction_date) FROM bank_contextual_event WHERE source_account = ? AND source_institution = ?",
        RowMapper { result: ResultSet, _: Int -> result.getObject(1, LocalDate::class.java) },
        transaction.sourceAccount,
        transaction.sourceInstitution,
    ).singleOrNull()

    /** Returns at most maxRows + 1 so the caller can reject overflow, never truncate. */
    fun history(transaction: BankContextualTransaction, maxRows: Int): List<BankContextualTransaction> = jdbc.query(
        """SELECT e.source_account, e.destination_account, e.source_institution,
                  e.destination_institution, e.transaction_date, d.amount, d.time_bucket,
                  d.fund_type, d.channel
           FROM bank_contextual_event e
           JOIN bank_decision d ON d.id = e.decision_id
           WHERE e.source_account = ? AND e.source_institution = ? AND e.transaction_date >= ? AND e.transaction_date < ?
           ORDER BY e.transaction_date, e.decision_id
           LIMIT ?""".trimIndent(),
        historyMapper,
        transaction.sourceAccount,
        transaction.sourceInstitution,
        transaction.transactionDate.minusDays(90),
        transaction.transactionDate,
        maxRows + 1,
    )

    fun priorSummary(transaction: BankContextualTransaction): BankPriorSummary = jdbc.query(
        """SELECT COUNT(*) AS sender_count,
                  COALESCE(SUM(d.amount), 0) AS amount_sum,
                  COALESCE(MAX(d.amount), 0) AS amount_max,
                  COUNT(DISTINCT ROW(e.destination_institution, e.destination_account)) AS distinct_recipients,
                  COALESCE(SUM(CASE WHEN e.destination_account = ? AND e.destination_institution = ? THEN 1 ELSE 0 END), 0) AS recipient_count,
                  COALESCE(SUM(CASE WHEN e.destination_institution = ? THEN 1 ELSE 0 END), 0) AS recipient_bank_count,
                  COALESCE(SUM(CASE WHEN d.channel = ? THEN 1 ELSE 0 END), 0) AS same_channel_count,
                  COALESCE(SUM(CASE WHEN d.fund_type = ? THEN 1 ELSE 0 END), 0) AS same_fund_count,
                  MIN(e.transaction_date) AS first_date,
                  MAX(e.transaction_date) AS last_date,
                  MAX(CASE WHEN e.destination_account = ? AND e.destination_institution = ? THEN e.transaction_date END) AS last_recipient_date
           FROM bank_contextual_event e JOIN bank_decision d ON d.id = e.decision_id
           WHERE e.source_account = ? AND e.source_institution = ? AND e.transaction_date < ?""".trimIndent(),
        RowMapper { result: ResultSet, _: Int ->
            BankPriorSummary(
                senderCount = result.getLong("sender_count"),
                senderAmountSum = result.getBigDecimal("amount_sum"),
                senderAmountMax = result.getBigDecimal("amount_max"),
                distinctRecipients = result.getLong("distinct_recipients"),
                recipientCount = result.getLong("recipient_count"),
                recipientBankCount = result.getLong("recipient_bank_count"),
                sameChannelCount = result.getLong("same_channel_count"),
                sameFundCount = result.getLong("same_fund_count"),
                firstDate = result.getObject("first_date", LocalDate::class.java),
                lastDate = result.getObject("last_date", LocalDate::class.java),
                lastRecipientDate = result.getObject("last_recipient_date", LocalDate::class.java),
            )
        },
        transaction.destinationAccount,
        transaction.destinationInstitution,
        transaction.destinationInstitution,
        transaction.channel,
        transaction.fundType,
        transaction.destinationAccount,
        transaction.destinationInstitution,
        transaction.sourceAccount,
        transaction.sourceInstitution,
        transaction.transactionDate,
    ).single()

    fun save(
        transaction: BankContextualTransaction,
        riskScore: Double,
        alert: Boolean,
        threshold: Double,
        modelVersion: String,
        requestKey: String,
        requestHash: String,
        historyCount: Int,
        historyWindowStart: LocalDate,
        historyStatus: String,
        contextStatus: String,
        evidenceType: String,
        priorSummary: BankPriorSummary,
        priorSummaryJson: String,
        featureSchemaVersion: String,
        observedContextJson: String,
        observedContext: Map<String, Double>,
        featureSnapshotJson: String,
        featureSnapshot: Map<String, Double>,
    ): BankContextualEvidence = transactionTemplate.execute {
        val decision = decisions.save(
            amount = transaction.amount,
            timeBucket = transaction.timeBucket,
            fundType = transaction.fundType,
            channel = transaction.channel,
            riskScore = riskScore,
            alert = alert,
            threshold = threshold,
            modelVersion = modelVersion,
            requestKey = requestKey,
            requestHash = requestHash,
        )
        jdbc.update(
            """INSERT INTO bank_contextual_event
                (decision_id, transaction_date, source_account, destination_account,
                 source_institution, destination_institution, history_count,
                 history_window_start, history_status, context_status, evidence_type,
                 feature_schema_version, prior_summary, observed_context, feature_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""".trimIndent(),
            decision.id,
            transaction.transactionDate,
            transaction.sourceAccount,
            transaction.destinationAccount,
            transaction.sourceInstitution,
            transaction.destinationInstitution,
            historyCount,
            historyWindowStart,
            historyStatus,
            contextStatus,
            evidenceType,
            featureSchemaVersion,
            priorSummaryJson,
            observedContextJson,
            featureSnapshotJson,
        )
        BankContextualEvidence(
            decision, transaction.transactionDate, historyCount, historyWindowStart,
            historyStatus, contextStatus, evidenceType, priorSummary,
            featureSchemaVersion, observedContext, featureSnapshot,
        )
    } ?: error("Contextual decision transaction returned no result")

    fun findEvidence(decisionId: String, mapper: tools.jackson.databind.ObjectMapper): BankContextualEvidence? {
        val stored = jdbc.query(
            """SELECT d.id, d.created_at, d.amount, d.time_bucket, d.fund_type, d.channel,
                      d.risk_score, d.alert, d.threshold, d.model_version,
                      e.transaction_date, e.history_count, e.history_window_start,
                      e.history_status, e.context_status, e.evidence_type, e.feature_schema_version,
                      e.prior_summary, e.observed_context, e.feature_snapshot
               FROM bank_contextual_event e JOIN bank_decision d ON d.id = e.decision_id
               WHERE e.decision_id = ?""".trimIndent(),
            evidenceMapper(mapper),
            decisionId,
        )
        return stored.singleOrNull()
    }

    fun findEvidencePage(
        limit: Int, alert: Boolean?, before: BankDecisionCursor?, mapper: tools.jackson.databind.ObjectMapper,
    ): List<BankContextualEvidence> {
        val conditions = mutableListOf<String>()
        val parameters = mutableListOf<Any>()
        if (alert != null) {
            conditions += "d.alert = ?"
            parameters += alert
        }
        if (before != null) {
            conditions += "(d.created_at < ? OR (d.created_at = ? AND d.id < ?))"
            parameters.addAll(listOf(before.createdAt, before.createdAt, before.id))
        }
        val where = if (conditions.isEmpty()) "" else "WHERE ${conditions.joinToString(" AND ")}"
        parameters += limit
        return jdbc.query(
            """SELECT d.id, d.created_at, d.amount, d.time_bucket, d.fund_type, d.channel,
                      d.risk_score, d.alert, d.threshold, d.model_version,
                      e.transaction_date, e.history_count, e.history_window_start,
                      e.history_status, e.context_status, e.evidence_type, e.feature_schema_version,
                      e.prior_summary, e.observed_context, e.feature_snapshot
               FROM bank_contextual_event e JOIN bank_decision d ON d.id = e.decision_id
               $where
               ORDER BY d.created_at DESC, d.id DESC
               LIMIT ?""".trimIndent(),
            evidenceMapper(mapper),
            *parameters.toTypedArray(),
        )
    }

    fun totals(): Pair<Long, Long> = jdbc.query(
        """SELECT COUNT(*) AS total_count,
                  COALESCE(SUM(CASE WHEN d.alert THEN 1 ELSE 0 END), 0) AS total_alerts
           FROM bank_contextual_event e JOIN bank_decision d ON d.id = e.decision_id""".trimIndent(),
        RowMapper { result: ResultSet, _: Int ->
            result.getLong("total_count") to result.getLong("total_alerts")
        },
    ).single()

    private fun evidenceMapper(mapper: tools.jackson.databind.ObjectMapper): RowMapper<BankContextualEvidence> =
        RowMapper { result: ResultSet, _: Int ->
            val snapshot = mapper.readTree(result.getString("feature_snapshot"))
            val observed = mapper.readTree(result.getString("observed_context"))
            val summary = mapper.readTree(result.getString("prior_summary"))
            fun date(name: String): LocalDate? = summary[name].takeUnless { it.isNull }?.let {
                LocalDate.parse(it.stringValue(), java.time.format.DateTimeFormatter.BASIC_ISO_DATE)
            }
            BankContextualEvidence(
                decision = BankDecision(
                    id = result.getString("id"),
                    createdAt = result.getObject("created_at", java.time.OffsetDateTime::class.java),
                    amount = result.getBigDecimal("amount"),
                    timeBucket = result.getInt("time_bucket"),
                    fundType = result.getString("fund_type"),
                    channel = result.getString("channel"),
                    riskScore = result.getDouble("risk_score"),
                    alert = result.getBoolean("alert"),
                    threshold = result.getDouble("threshold"),
                    modelVersion = result.getString("model_version"),
                ),
                transactionDate = result.getObject("transaction_date", LocalDate::class.java),
                historyCount = result.getInt("history_count"),
                historyWindowStart = result.getObject("history_window_start", LocalDate::class.java),
                historyStatus = result.getString("history_status"),
                contextStatus = result.getString("context_status"),
                evidenceType = result.getString("evidence_type"),
                priorSummary = BankPriorSummary(
                    senderCount = summary["senderCount"].longValue(),
                    senderAmountSum = summary["senderAmountSum"].decimalValue(),
                    senderAmountMax = summary["senderAmountMax"].decimalValue(),
                    distinctRecipients = summary["distinctRecipients"].longValue(),
                    recipientCount = summary["recipientCount"].longValue(),
                    recipientBankCount = summary["recipientBankCount"].longValue(),
                    sameChannelCount = summary["sameChannelCount"].longValue(),
                    sameFundCount = summary["sameFundCount"].longValue(),
                    firstDate = date("firstDate"),
                    lastDate = date("lastDate"),
                    lastRecipientDate = date("lastRecipientDate"),
                ),
                featureSchemaVersion = result.getString("feature_schema_version"),
                observedContext = observed.properties().associate { it.key to it.value.doubleValue() },
                featureSnapshot = snapshot.properties().associate { it.key to it.value.doubleValue() },
            )
        }

    private companion object {
        fun requiredDataSource(jdbc: JdbcTemplate): DataSource =
            requireNotNull(jdbc.dataSource) { "A DataSource is required for atomic contextual decisions" }
    }
}
