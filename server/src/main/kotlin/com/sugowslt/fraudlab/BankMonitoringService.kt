package com.sugowslt.fraudlab

import java.nio.file.Files
import java.nio.file.Path
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneOffset
import kotlin.math.abs
import kotlin.math.ln
import org.springframework.beans.factory.annotation.Value
import org.springframework.http.HttpStatus
import org.springframework.stereotype.Service
import org.springframework.web.server.ResponseStatusException
import tools.jackson.databind.JsonNode
import tools.jackson.databind.ObjectMapper

data class DistributionPoint(
    val label: String,
    val referenceShare: Double,
    val observedShare: Double,
)

data class BankFeatureDrift(
    val feature: String,
    val metric: String,
    val score: Double?,
    val status: String,
    val largestShift: DistributionPoint?,
    val distribution: List<DistributionPoint>,
)

data class BankModelVersionSummary(
    val modelVersion: String,
    val decisions: Int,
    val alerts: Int,
    val alertRate: Double,
)

data class BankMonitoringPeriod(
    val date: LocalDate,
    val decisions: Int,
    val alerts: Int,
    val alertRate: Double,
    val averageRiskScore: Double,
)

data class BankReferenceVersion(
    val version: String,
    val source: String,
    val rows: Int,
    val changeReason: String,
    val recordedAt: String?,
    val current: Boolean,
    val reference: JsonNode,
)

data class BankMonitoringSnapshot(
    val generatedAt: OffsetDateTime,
    val sampleSize: Int,
    val windowLimit: Int,
    val minimumSampleSize: Int,
    val ready: Boolean,
    val referenceSource: String,
    val referenceVersion: String,
    val referenceVersions: List<BankReferenceVersion>,
    val drift: List<BankFeatureDrift>,
    val timeline: List<BankMonitoringPeriod>,
    val modelVersions: List<BankModelVersionSummary>,
)

@Service
class BankMonitoringService(
    private val repository: BankDecisionRepository,
    private val objectMapper: ObjectMapper,
    @Value("\${fraud.reports-dir}") private val reportsDir: String,
) {
    companion object {
        const val WINDOW_LIMIT = 1_000
        const val MINIMUM_SAMPLE_SIZE = 30
        const val TIMELINE_ACTIVE_DAYS = 14
        private const val EPSILON = 1e-6
    }

    fun snapshot(): BankMonitoringSnapshot {
        val decisions = repository.findRecentForMonitoring(WINDOW_LIMIT)
        val report = readReport()
        val reference = report["monitoring_reference"]
        if (reference == null || reference.isMissingNode) {
            throw ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Monitoring reference not found")
        }
        val history = report["monitoring_reference_history"]
        val versions = (history?.values()?.map { referenceVersion(it, false) } ?: emptyList()) +
            referenceVersion(reference, true)
        val ready = decisions.size >= MINIMUM_SAMPLE_SIZE
        return BankMonitoringSnapshot(
            generatedAt = OffsetDateTime.now(ZoneOffset.UTC),
            sampleSize = decisions.size,
            windowLimit = WINDOW_LIMIT,
            minimumSampleSize = MINIMUM_SAMPLE_SIZE,
            ready = ready,
            referenceSource = reference["source"].stringValue(),
            referenceVersion = versions.last().version,
            referenceVersions = versions,
            drift = listOf(
                amountDrift(decisions, reference["amount_bands"], ready),
                categoryDrift("거래시간대", decisions.map { it.timeBucket.toString() }, reference, ready),
                categoryDrift("자금구분", decisions.map { it.fundType }, reference, ready),
                categoryDrift("매체구분", decisions.map { it.channel }, reference, ready),
            ),
            timeline = decisions
                .groupBy { it.createdAt.withOffsetSameInstant(ZoneOffset.UTC).toLocalDate() }
                .toSortedMap()
                .entries
                .toList()
                .takeLast(TIMELINE_ACTIVE_DAYS)
                .map { (date, rows) ->
                    val alerts = rows.count { it.alert }
                    BankMonitoringPeriod(
                        date = date,
                        decisions = rows.size,
                        alerts = alerts,
                        alertRate = alerts.toDouble() / rows.size,
                        averageRiskScore = rows.map { it.riskScore }.average(),
                    )
                },
            modelVersions = decisions.groupBy { it.modelVersion }
                .map { (version, rows) ->
                    val alerts = rows.count { it.alert }
                    BankModelVersionSummary(version, rows.size, alerts, alerts.toDouble() / rows.size)
                }
                .sortedWith(compareByDescending<BankModelVersionSummary> { it.decisions }.thenBy { it.modelVersion }),
        )
    }

    private fun readReport(): JsonNode {
        val path = Path.of(reportsDir, "bank_baseline.json")
        if (!Files.isRegularFile(path)) {
            throw ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Bank baseline report not found")
        }
        return objectMapper.readTree(Files.readString(path))
    }

    private fun referenceVersion(reference: JsonNode, current: Boolean): BankReferenceVersion {
        val version = reference["version"]?.stringValue()
        val reason = reference["change_reason"]?.stringValue()
        if (version.isNullOrBlank() || reason.isNullOrBlank()) {
            throw ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Invalid monitoring reference version")
        }
        return BankReferenceVersion(
            version = version,
            source = reference["source"].stringValue(),
            rows = reference["rows"].intValue(),
            changeReason = reason,
            recordedAt = reference["recorded_at"]?.stringValue(),
            current = current,
            reference = reference,
        )
    }

    private fun amountDrift(
        decisions: List<BankMonitoringDecision>,
        reference: JsonNode,
        ready: Boolean,
    ): BankFeatureDrift {
        val bounds = reference["upper_bounds"].values().map { it.longValue() }
        val expected = reference["proportions"].values().map { it.doubleValue() }
        if (expected.size != bounds.size + 1) {
            throw ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Invalid amount monitoring reference")
        }
        val counts = IntArray(expected.size)
        decisions.forEach { decision ->
            val band = bounds.indexOfFirst { decision.amount <= it.toBigDecimal() }
            counts[if (band == -1) bounds.size else band] += 1
        }
        val observed = counts.map { count -> share(count, decisions.size) }
        val labels = bounds.indices.map { index ->
            if (index == 0) "≤${bounds[index]}" else "${bounds[index - 1] + 1}–${bounds[index]}"
        } + ">${bounds.last()}"
        val score = if (decisions.isEmpty()) null else expected.indices.sumOf { index ->
            val expectedShare = expected[index].coerceAtLeast(EPSILON)
            val observedShare = observed[index].coerceAtLeast(EPSILON)
            (observedShare - expectedShare) * ln(observedShare / expectedShare)
        }
        return drift("거래금액", "PSI", score, ready, labels, expected, observed, 0.1, 0.25)
    }

    private fun categoryDrift(
        feature: String,
        values: List<String>,
        reference: JsonNode,
        ready: Boolean,
    ): BankFeatureDrift {
        val expectedNode = reference["categories"][feature]
        val labelNode = reference["category_labels"]?.get(feature)
        val expectedByValue = linkedMapOf<String, Double>()
        expectedNode.properties().forEach { (key, value) ->
            val label = labelNode?.get(key)?.stringValue() ?: key
            expectedByValue[label] = value.doubleValue()
        }
        values.filterNot { it in expectedByValue }.distinct().sorted().forEach { expectedByValue[it] = 0.0 }
        val observedCounts = values.groupingBy { it }.eachCount()
        val labels = expectedByValue.keys.toList()
        val expected = expectedByValue.values.toList()
        val observed = labels.map { share(observedCounts[it] ?: 0, values.size) }
        val score = if (values.isEmpty()) null else expected.indices.sumOf { abs(observed[it] - expected[it]) } / 2.0
        return drift(feature, "TVD", score, ready, labels, expected, observed, 0.1, 0.2)
    }

    private fun drift(
        feature: String,
        metric: String,
        score: Double?,
        ready: Boolean,
        labels: List<String>,
        expected: List<Double>,
        observed: List<Double>,
        warningThreshold: Double,
        driftThreshold: Double,
    ): BankFeatureDrift {
        val distribution = labels.indices.map { index ->
            DistributionPoint(labels[index], expected[index], observed[index])
        }
        val status = when {
            !ready -> "INSUFFICIENT_DATA"
            score == null || score < warningThreshold -> "STABLE"
            score < driftThreshold -> "WATCH"
            else -> "DRIFT"
        }
        return BankFeatureDrift(
            feature = feature,
            metric = metric,
            score = score,
            status = status,
            largestShift = if (score == null) null else distribution.maxByOrNull { abs(it.observedShare - it.referenceShare) },
            distribution = distribution,
        )
    }

    private fun share(count: Int, total: Int): Double = if (total == 0) 0.0 else count.toDouble() / total
}
