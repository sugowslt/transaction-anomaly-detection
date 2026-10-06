package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.sql.ResultSet
import java.time.LocalDate
import java.time.LocalDateTime
import javax.sql.DataSource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.jdbc.datasource.DataSourceTransactionManager
import org.springframework.stereotype.Repository
import org.springframework.transaction.support.TransactionTemplate

data class BankGraphEvidence(
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
    val graphContext: Map<String, Any>,
    val modelExplanation: BankModelExplanation?,
    val supportAssessment: BankSupportAssessment?,
    val featureSnapshot: Map<String, Double>,
)

@Repository
class BankGraphEventRepository(
    private val jdbc: JdbcTemplate,
    private val decisions: BankDecisionRepository,
) {
    private val transactionTemplate = TransactionTemplate(DataSourceTransactionManager(requiredDataSource(jdbc)))
    private val namedJdbc = NamedParameterJdbcTemplate(jdbc)
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

    fun latestBucket(): LocalDateTime? = jdbc.query(
        "SELECT transaction_date, transaction_hour FROM bank_graph_event ORDER BY transaction_date DESC, transaction_hour DESC LIMIT 1",
        RowMapper { result: ResultSet, _: Int -> result.getObject(1, LocalDate::class.java).atTime(result.getInt(2), 0) },
    ).singleOrNull()

    fun windowClosed(date: LocalDate, hour: Int): Boolean = (jdbc.queryForObject(
        "SELECT COUNT(*) FROM bank_closed_window_review WHERE transaction_date = ? AND transaction_hour = ?",
        Int::class.java, date, hour,
    ) ?: 0) > 0

    /** Server-owned observations from strictly earlier (date, bucket) events. */
    fun graphContext(transaction: BankContextualTransaction): Map<String, Any> {
        val now = transaction.transactionDate.atTime(transaction.timeBucket, 0)
        val parameters = mutableMapOf<String, Any>(
            "sender" to transaction.sourceAccount, "recipient" to transaction.destinationAccount,
            "senderInstitution" to transaction.sourceInstitution, "recipientInstitution" to transaction.destinationInstitution,
            "day" to transaction.transactionDate, "hour" to transaction.timeBucket,
        )
        for (days in listOf(1, 7, 30, 90)) {
            val lower = now.minusDays(days.toLong())
            parameters["lower${days}Date"] = lower.toLocalDate()
            parameters["lower${days}Hour"] = lower.hour
        }
        fun lower(days: Int) = "(e.transaction_date > :lower${days}Date OR (e.transaction_date = :lower${days}Date AND e.transaction_hour >= :lower${days}Hour))"
        val prior = "(e.transaction_date < :day OR (e.transaction_date = :day AND e.transaction_hour < :hour))"
        val day = "e.transaction_date = :day"
        fun aggregate(side: String, node: String, selections: List<String>): Map<String, Any> = namedJdbc.query(
            "SELECT ${selections.joinToString(", ")} FROM bank_graph_event e JOIN bank_decision d ON d.id = e.decision_id " +
                "WHERE e.$side = :$node AND e.${if (side == "source_account") "source_institution" else "destination_institution"} = :${node}Institution AND ${lower(90)} AND $prior",
            parameters,
            RowMapper { result: ResultSet, _: Int ->
                val fields = result.metaData
                (1..fields.columnCount).associate { index ->
                    val key = fields.getColumnLabel(index)
                    key to if (key.endsWith("Amount")) result.getDouble(index) else result.getLong(index)
                }
            },
        ).single()
        fun count(condition: String, name: String) = "COALESCE(SUM(CASE WHEN $condition THEN 1 ELSE 0 END), 0) AS \"$name\""
        fun amount(condition: String, name: String) = "COALESCE(SUM(CASE WHEN $condition THEN d.amount ELSE 0 END), 0) AS \"$name\""
        val outgoingSender = aggregate("source_account", "sender", listOf(
            count(day, "senderDayCount"), amount(day, "senderDayAmount"),
            "COUNT(DISTINCT CASE WHEN $day THEN ROW(e.destination_institution, e.destination_account) END) AS \"senderDayDistinctRecipients\"",
            count("$day AND e.destination_account = :recipient AND e.destination_institution = :recipientInstitution", "senderPairDayCount"),
            count(lower(1), "sender24hCount"), amount(lower(1), "sender24hAmount"),
        ))
        fun incoming(node: String, prefix: String) = aggregate("destination_account", node, listOf(
            count(lower(7), "${prefix}Inflow7dCount"), count(lower(30), "${prefix}Inflow30dCount"),
            "COUNT(*) AS \"${prefix}Inflow90dCount\"", "COALESCE(SUM(d.amount), 0) AS \"${prefix}Inflow90dAmount\"",
            "COUNT(DISTINCT ROW(e.source_institution, e.source_account)) AS \"${prefix}Inflow90dDistinctSources\"",
            count(day, "${prefix}DayInflowCount"), amount(day, "${prefix}DayInflowAmount"),
        ))
        val outgoingRecipient = aggregate("source_account", "recipient", listOf(
            "COUNT(*) AS \"recipientOutflow90dCount\"", "COALESCE(SUM(d.amount), 0) AS \"recipientOutflow90dAmount\"",
            count(day, "recipientDayOutflowCount"), amount(day, "recipientDayOutflowAmount"),
        ))
        return linkedMapOf<String, Any>(
            "asOfDate" to transaction.transactionDate.format(java.time.format.DateTimeFormatter.BASIC_ISO_DATE),
            "asOfHour" to transaction.timeBucket,
        ).apply {
            putAll(outgoingSender)
            putAll(incoming("sender", "sender"))
            putAll(incoming("recipient", "recipient"))
            putAll(outgoingRecipient)
        }
    }

    /** Returns at most maxRows + 1 so the caller can reject overflow, never truncate. */
    fun history(transaction: BankContextualTransaction, maxRows: Int): List<BankContextualTransaction> = jdbc.query(
        """SELECT e.source_account, e.destination_account, e.source_institution,
                  e.destination_institution, e.transaction_date, d.amount, d.time_bucket,
                  d.fund_type, d.channel
           FROM bank_graph_event e
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
           FROM bank_graph_event e JOIN bank_decision d ON d.id = e.decision_id
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
        graphContextJson: String,
        graphContext: Map<String, Any>,
        modelExplanationJson: String,
        modelExplanation: BankModelExplanation,
        supportAssessmentJson: String,
        supportAssessment: BankSupportAssessment,
        featureSnapshotJson: String,
        featureSnapshot: Map<String, Double>,
    ): BankGraphEvidence = transactionTemplate.execute {
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
            """INSERT INTO bank_graph_event
                (decision_id, transaction_date, transaction_hour, source_account, destination_account,
                 source_institution, destination_institution, history_count,
                 history_window_start, history_status, context_status, evidence_type,
                 feature_schema_version, prior_summary, observed_context, graph_context, model_explanation, support_assessment, feature_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""".trimIndent(),
            decision.id,
            transaction.transactionDate,
            transaction.timeBucket,
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
            graphContextJson,
            modelExplanationJson,
            supportAssessmentJson,
            featureSnapshotJson,
        )
        BankGraphEvidence(
            decision, transaction.transactionDate, historyCount, historyWindowStart,
            historyStatus, contextStatus, evidenceType, priorSummary,
            featureSchemaVersion, observedContext, graphContext, modelExplanation, supportAssessment, featureSnapshot,
        )
    } ?: error("Contextual decision transaction returned no result")

    fun findEvidence(decisionId: String, mapper: tools.jackson.databind.ObjectMapper): BankGraphEvidence? {
        val stored = jdbc.query(
            """SELECT d.id, d.created_at, d.amount, d.time_bucket, d.fund_type, d.channel,
                      d.risk_score, d.alert, d.threshold, d.model_version,
                      e.transaction_date, e.history_count, e.history_window_start,
                      e.history_status, e.context_status, e.evidence_type, e.feature_schema_version,
                      e.prior_summary, e.observed_context, e.graph_context, e.model_explanation, e.support_assessment, e.feature_snapshot
               FROM bank_graph_event e JOIN bank_decision d ON d.id = e.decision_id
               WHERE e.decision_id = ?""".trimIndent(),
            evidenceMapper(mapper),
            decisionId,
        )
        return stored.singleOrNull()
    }

    fun findEvidencePage(
        limit: Int, alert: Boolean?, before: BankDecisionCursor?, mapper: tools.jackson.databind.ObjectMapper,
    ): List<BankGraphEvidence> {
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
                      e.prior_summary, e.observed_context, e.graph_context, e.model_explanation, e.support_assessment, e.feature_snapshot
               FROM bank_graph_event e JOIN bank_decision d ON d.id = e.decision_id
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
           FROM bank_graph_event e JOIN bank_decision d ON d.id = e.decision_id""".trimIndent(),
        RowMapper { result: ResultSet, _: Int ->
            result.getLong("total_count") to result.getLong("total_alerts")
        },
    ).single()

    private fun evidenceMapper(mapper: tools.jackson.databind.ObjectMapper): RowMapper<BankGraphEvidence> =
        RowMapper { result: ResultSet, _: Int ->
            val snapshot = mapper.readTree(result.getString("feature_snapshot"))
            val observed = mapper.readTree(result.getString("observed_context"))
            val graph = mapper.readTree(result.getString("graph_context"))
            val summary = mapper.readTree(result.getString("prior_summary"))
            val explanation = result.getString("model_explanation")?.let { BankModelExplanation.fromJson(mapper.readTree(it)) }
            val support = result.getString("support_assessment")?.let { BankSupportAssessment.fromJson(mapper.readTree(it)) }
            fun date(name: String): LocalDate? = summary[name].takeUnless { it.isNull }?.let {
                LocalDate.parse(it.stringValue(), java.time.format.DateTimeFormatter.BASIC_ISO_DATE)
            }
            BankGraphEvidence(
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
                graphContext = graph.properties().associate { entry ->
                    entry.key to when {
                        entry.key == "asOfDate" -> entry.value.stringValue()
                        entry.key == "asOfHour" -> entry.value.intValue()
                        entry.key.endsWith("Amount") -> entry.value.doubleValue()
                        else -> entry.value.longValue()
                    }
                },
                featureSnapshot = snapshot.properties().associate { it.key to it.value.doubleValue() },
                modelExplanation = explanation,
                supportAssessment = support,
            )
        }

    private companion object {
        fun requiredDataSource(jdbc: JdbcTemplate): DataSource =
            requireNotNull(jdbc.dataSource) { "A DataSource is required for atomic contextual decisions" }
    }
}
