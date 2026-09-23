package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.sql.ResultSet
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.util.UUID
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository

data class BankDecision(
    val id: String,
    val createdAt: OffsetDateTime,
    val amount: BigDecimal,
    val timeBucket: Int,
    val fundType: String,
    val channel: String,
    val riskScore: Double,
    val alert: Boolean,
    val threshold: Double,
    val modelVersion: String,
)

data class BankMonitoringDecision(
    val amount: BigDecimal,
    val timeBucket: Int,
    val fundType: String,
    val channel: String,
    val alert: Boolean,
    val modelVersion: String,
)

@Repository
class BankDecisionRepository(private val jdbc: JdbcTemplate) {
    private val rowMapper = RowMapper { result: ResultSet, _: Int ->
        BankDecision(
            id = result.getString("id"),
            createdAt = result.getObject("created_at", OffsetDateTime::class.java),
            amount = result.getBigDecimal("amount"),
            timeBucket = result.getInt("time_bucket"),
            fundType = result.getString("fund_type"),
            channel = result.getString("channel"),
            riskScore = result.getDouble("risk_score"),
            alert = result.getBoolean("alert"),
            threshold = result.getDouble("threshold"),
            modelVersion = result.getString("model_version"),
        )
    }
    private val monitoringRowMapper = RowMapper { result: ResultSet, _: Int ->
        BankMonitoringDecision(
            amount = result.getBigDecimal("amount"),
            timeBucket = result.getInt("time_bucket"),
            fundType = result.getString("fund_type"),
            channel = result.getString("channel"),
            alert = result.getBoolean("alert"),
            modelVersion = result.getString("model_version"),
        )
    }

    fun save(
        amount: BigDecimal,
        timeBucket: Int,
        fundType: String,
        channel: String,
        riskScore: Double,
        alert: Boolean,
        threshold: Double,
        modelVersion: String,
    ): BankDecision {
        val decision = BankDecision(
            id = UUID.randomUUID().toString(),
            createdAt = OffsetDateTime.now(ZoneOffset.UTC),
            amount = amount,
            timeBucket = timeBucket,
            fundType = fundType,
            channel = channel,
            riskScore = riskScore,
            alert = alert,
            threshold = threshold,
            modelVersion = modelVersion,
        )
        jdbc.update(
            """INSERT INTO bank_decision
                (id, created_at, amount, time_bucket, fund_type, channel, risk_score, alert, threshold, model_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""".trimIndent(),
            decision.id,
            decision.createdAt,
            decision.amount,
            decision.timeBucket,
            decision.fundType,
            decision.channel,
            decision.riskScore,
            decision.alert,
            decision.threshold,
            decision.modelVersion,
        )
        return decision
    }

    fun findAlerts(limit: Int): List<BankDecision> = jdbc.query(
        """SELECT id, created_at, amount, time_bucket, fund_type, channel,
                  risk_score, alert, threshold, model_version
           FROM bank_decision
           WHERE alert = TRUE
           ORDER BY created_at DESC
           LIMIT ?""".trimIndent(),
        rowMapper,
        limit,
    )

    fun findRecentForMonitoring(limit: Int): List<BankMonitoringDecision> = jdbc.query(
        """SELECT amount, time_bucket, fund_type, channel, alert, model_version
           FROM bank_decision
           ORDER BY created_at DESC
           LIMIT ?""".trimIndent(),
        monitoringRowMapper,
        limit,
    )
}
