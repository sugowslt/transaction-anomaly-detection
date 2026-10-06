package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import java.time.Duration
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.temporal.ChronoUnit
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.time.format.ResolverStyle
import java.util.HexFormat
import java.util.Base64
import java.util.UUID
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import kotlin.math.abs
import kotlin.math.max
import org.springframework.beans.factory.annotation.Value
import org.springframework.dao.DataIntegrityViolationException
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestHeader
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import tools.jackson.core.JacksonException
import tools.jackson.databind.JsonNode
import tools.jackson.databind.ObjectMapper
import tools.jackson.databind.cfg.JsonNodeFeature

data class BankGraphDecisionPage(
    val items: List<BankGraphEvidence>,
    val nextCursor: String?,
    val totalCount: Long,
    val totalAlerts: Long,
)

/**
 * Candidate scoring with server-owned node flow observations. Separate routes
 * and a separate ledger preserve the v1 prior-day decision contract.
 */
@RestController
@RequestMapping("/api/bank")
class BankGraphEventController(
    @Value("\${fraud.model-url}") private val modelUrl: String,
    private val decisions: BankDecisionRepository,
    private val contextual: BankGraphEventRepository,
    private val mapper: ObjectMapper,
    @Value("\${fraud.bank-v2-candidate-enabled:false}") private val enabled: Boolean = false,
    @Value("\${fraud.reports-dir:../reports}") private val reportsDir: String = "../reports",
) {
    private val reader = mapper.reader().with(JsonNodeFeature.USE_BIG_DECIMAL_FOR_FLOATS)
    private val client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build()

    @GetMapping("/contextual-v2-capabilities", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun capabilities(): Map<String, Boolean> = mapOf("enabled" to enabled, "releaseReady" to false)

    @PostMapping("/contextual-v2-events", consumes = [MediaType.APPLICATION_JSON_VALUE], produces = [MediaType.APPLICATION_JSON_VALUE])
    fun scoreAndStore(
        @RequestBody body: String,
        @RequestHeader("Idempotency-Key", required = false) requestKey: String?,
    ): ResponseEntity<String> {
        if (!enabled) return error(HttpStatus.SERVICE_UNAVAILABLE, "Candidate bank model path is disabled pending model validation")
        val bytes = body.toByteArray(StandardCharsets.UTF_8)
        if (bytes.size !in 1..65_536) return error(HttpStatus.BAD_REQUEST, "Request body size must be 1-65536 bytes")
        if (requestKey == null || !requestKey.matches(Regex("[A-Za-z0-9._~-]{1,64}"))) {
            return error(HttpStatus.BAD_REQUEST, "Valid Idempotency-Key is required")
        }
        val hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes))
        replay(requestKey, hash)?.let { return it }
        val input = try {
            parseTransaction(reader.readTree(body))
        } catch (exception: JacksonException) {
            null
        } ?: return error(HttpStatus.BAD_REQUEST, "Invalid contextual bank transaction")

        // A graph observation depends on other senders too. One process owns
        // the file-backed H2 ledger, and the global lock serializes its complete
        // history read -> model call -> save cycle.
        return graphLock.withLock {
            replay(requestKey, hash)?.let { return@withLock it }
            if (contextual.windowClosed(input.transactionDate, input.timeBucket)) {
                return@withLock error(HttpStatus.CONFLICT, "This observation window was closed; new events cannot change its review")
            }
            val latest = contextual.latestBucket()
            if (latest != null && input.transactionDate.atTime(input.timeBucket, 0).isBefore(latest)) {
                return@withLock error(HttpStatus.CONFLICT, "A later graph transaction bucket was already scored")
            }
            val history = contextual.history(input, MAX_HISTORY_ROWS)
            if (history.size > MAX_HISTORY_ROWS) {
                return@withLock error(HttpStatus.PAYLOAD_TOO_LARGE, "Sender history exceeds 20000 transactions")
            }
            val priorSummary = contextual.priorSummary(input)
            val graphContext = contextual.graphContext(input)
            val payload = mapper.writeValueAsString(mapOf(
                "transaction" to input.modelInput(),
                "history" to history.map { it.modelInput() },
                "priorSummary" to priorSummary.modelInput(),
                "graphContext" to graphContext,
            ))
            if (payload.toByteArray(StandardCharsets.UTF_8).size > MAX_MODEL_BODY_BYTES) {
                return@withLock error(HttpStatus.PAYLOAD_TOO_LARGE, "Sender history exceeds model request size")
            }
            val request = HttpRequest.newBuilder(URI.create(modelUrl.trimEnd('/') + "/score/bank/contextual-v2"))
                .timeout(Duration.ofSeconds(15))
                .header("Content-Type", MediaType.APPLICATION_JSON_VALUE)
                .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8))
                .build()
            val response = try {
                client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8))
            } catch (exception: InterruptedException) {
                Thread.currentThread().interrupt()
                return@withLock error(HttpStatus.SERVICE_UNAVAILABLE, "Model service unavailable")
            } catch (exception: java.io.IOException) {
                return@withLock error(HttpStatus.SERVICE_UNAVAILABLE, "Model service unavailable")
            }
            if (response.statusCode() !in 200..299) {
                return@withLock error(HttpStatus.BAD_GATEWAY, "Model service failed")
            }
            if (response.body().toByteArray(StandardCharsets.UTF_8).size > 65_536) {
                return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid model response")
            }
            val model = try {
                mapper.readTree(response.body())
            } catch (exception: JacksonException) {
                return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid model response")
            }
            val evidenceValues = validModelResponse(model, input, history, priorSummary, graphContext)
                ?: return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid model response")
            if (!validGraphResponse(model["graphContext"], graphContext)) {
                return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid model graph observations")
            }
            val explanation = BankModelExplanationProtocol.validate(model["modelExplanation"], model["riskScore"].doubleValue(), FEATURE_FIELDS)
                ?: return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid model score explanation")
            val support = BankSupportAssessment.validate(model["supportAssessment"], input, model["contextStatus"].stringValue())
                ?: return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid model support assessment")
            val historyStatus = if (priorSummary.senderCount == 0L) "prior_day_cold_start" else "local_history_unverified"
            val evidence = try {
                contextual.save(
                    transaction = input,
                    riskScore = model["riskScore"].doubleValue(),
                    alert = model["alert"].booleanValue(),
                    threshold = model["threshold"].doubleValue(),
                    modelVersion = model["modelVersion"].stringValue(),
                    requestKey = requestKey,
                    requestHash = hash,
                    historyCount = history.size,
                    historyWindowStart = input.transactionDate.minusDays(90),
                    historyStatus = historyStatus,
                    contextStatus = model["contextStatus"].stringValue(),
                    evidenceType = model["evidenceType"].stringValue(),
                    priorSummary = priorSummary,
                    priorSummaryJson = mapper.writeValueAsString(priorSummary.modelInput()),
                    featureSchemaVersion = model["featureSchemaVersion"].stringValue(),
                    observedContextJson = mapper.writeValueAsString(evidenceValues.first),
                    observedContext = evidenceValues.first,
                    graphContextJson = mapper.writeValueAsString(graphContext),
                    graphContext = graphContext,
                    modelExplanationJson = mapper.writeValueAsString(explanation),
                    modelExplanation = explanation,
                    supportAssessmentJson = mapper.writeValueAsString(support),
                    supportAssessment = support,
                    featureSnapshotJson = mapper.writeValueAsString(evidenceValues.second),
                    featureSnapshot = evidenceValues.second,
                )
            } catch (exception: DataIntegrityViolationException) {
                return@withLock replay(requestKey, hash) ?: throw exception
            }
            val stored = contextual.findEvidence(evidence.decision.id, mapper)
                ?: error("Saved contextual evidence could not be loaded")
            ResponseEntity.status(HttpStatus.CREATED).contentType(MediaType.APPLICATION_JSON)
                .body(mapper.writeValueAsString(stored))
        }
    }

    @GetMapping("/contextual-v2-metrics", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun metrics(): ResponseEntity<String> {
        val path = Path.of(reportsDir, "bank_contextual_v2_candidate.json")
        if (!Files.isRegularFile(path)) return error(HttpStatus.NOT_FOUND, "Candidate report not found")
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(Files.readString(path))
    }

    @GetMapping("/contextual-v2-decisions/{id}", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun evidence(@PathVariable id: String): ResponseEntity<String> {
        val decisionId = try {
            UUID.fromString(id).toString()
        } catch (exception: IllegalArgumentException) {
            return error(HttpStatus.BAD_REQUEST, "Invalid decision ID")
        }
        val found = contextual.findEvidence(decisionId, mapper)
            ?: return error(HttpStatus.NOT_FOUND, "Contextual decision not found")
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON)
            .body(mapper.writeValueAsString(found))
    }

    @GetMapping("/contextual-v2-decisions", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun evidencePage(
        @RequestParam(defaultValue = "20") limit: Int,
        @RequestParam(required = false) alert: Boolean? = null,
        @RequestParam(required = false) cursor: String? = null,
    ): BankGraphDecisionPage {
        if (limit !in 1..100) {
            throw org.springframework.web.server.ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid contextual page parameters")
        }
        val pageCursor = cursor?.let { decodeCursor(it) }
        val rows = contextual.findEvidencePage(limit + 1, alert, pageCursor, mapper)
        val items = rows.take(limit)
        val next = if (rows.size > limit) items.last().decision.let { encodeCursor(it.createdAt, it.id) } else null
        val totals = contextual.totals()
        return BankGraphDecisionPage(items, next, totals.first, totals.second)
    }

    private fun encodeCursor(createdAt: OffsetDateTime, id: String): String =
        Base64.getUrlEncoder().withoutPadding().encodeToString("$createdAt|$id".toByteArray(StandardCharsets.UTF_8))

    private fun decodeCursor(cursor: String): BankDecisionCursor {
        if (cursor.length !in 1..256) {
            throw org.springframework.web.server.ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid contextual cursor")
        }
        val parts = try {
            String(Base64.getUrlDecoder().decode(cursor), StandardCharsets.UTF_8).split('|')
        } catch (exception: IllegalArgumentException) {
            throw org.springframework.web.server.ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid contextual cursor")
        }
        if (parts.size != 2) {
            throw org.springframework.web.server.ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid contextual cursor")
        }
        val createdAt = try {
            OffsetDateTime.parse(parts[0])
        } catch (exception: DateTimeParseException) {
            throw org.springframework.web.server.ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid contextual cursor")
        }
        val id = try {
            UUID.fromString(parts[1]).toString()
        } catch (exception: IllegalArgumentException) {
            throw org.springframework.web.server.ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid contextual cursor")
        }
        return BankDecisionCursor(createdAt, id)
    }

    private fun parseTransaction(node: JsonNode?): BankContextualTransaction? {
        if (node == null || !node.isObject || node.properties().map { it.key }.toSet() != TRANSACTION_FIELDS) return null
        val ids = listOf("출금계좌일련번호", "입금계좌일련번호", "출금금융회사일련번호", "입금금융회사일련번호")
        if (ids.any { node[it]?.isString != true || !node[it].stringValue().matches(IDENTIFIER) }) return null
        val amount = node["거래금액"]
        val hour = node["거래시간대"]
        val fund = node["자금구분"]
        val channel = node["매체구분"]
        val date = node["거래일자"]
        if (amount?.isNumber != true || hour?.isNumber != true || fund?.isString != true ||
            channel?.isString != true || date?.isString != true) return null
        val value = amount.decimalValue()
        if (value < BigDecimal.ZERO || value >= MAX_AMOUNT || value.stripTrailingZeros().scale() > 2 ||
            hour.doubleValue() !in HOUR_CODES || fund.stringValue() !in FUND_TYPES ||
            channel.stringValue() !in CHANNELS) return null
        val transactionDate = try {
            LocalDate.parse(date.stringValue(), DATE_FORMAT)
        } catch (exception: DateTimeParseException) {
            return null
        }
        return BankContextualTransaction(
            sourceAccount = node["출금계좌일련번호"].stringValue(),
            destinationAccount = node["입금계좌일련번호"].stringValue(),
            sourceInstitution = node["출금금융회사일련번호"].stringValue(),
            destinationInstitution = node["입금금융회사일련번호"].stringValue(),
            fundType = fund.stringValue(),
            amount = value,
            timeBucket = hour.intValue(),
            channel = channel.stringValue(),
            transactionDate = transactionDate,
        )
    }

    private fun validModelResponse(
        model: JsonNode?, transaction: BankContextualTransaction,
        history: List<BankContextualTransaction>, priorSummary: BankPriorSummary,
        graphContext: Map<String, Any>,
    ): Pair<Map<String, Double>, Map<String, Double>>? {
        if (model == null || !model.isObject) return null
        val risk = model["riskScore"]
        val alert = model["alert"]
        val threshold = model["threshold"]
        val version = model["modelVersion"]
        val schema = model["featureSchemaVersion"]
        val count = model["historyCount"]
        val window = model["historyWindowStart"]
        val context = model["contextStatus"]
        val evidenceType = model["evidenceType"]
        val observed = model["observedContext"]
        val snapshot = model["featureSnapshot"]
        if (risk?.isNumber != true || alert?.isBoolean != true || threshold?.isNumber != true ||
            version?.isString != true || schema?.isString != true || count?.isNumber != true ||
            window?.isString != true || context?.isString != true || evidenceType?.isString != true ||
            observed?.isObject != true || snapshot?.isObject != true) return null
        if (!risk.doubleValue().isFinite() || risk.doubleValue() !in 0.0..1.0 ||
            !threshold.doubleValue().isFinite() || threshold.doubleValue() !in 0.0..1.0 ||
            alert.booleanValue() != (risk.doubleValue() >= threshold.doubleValue()) ||
            version.stringValue().isBlank() || version.stringValue().length > 64 ||
            schema.stringValue() != "bank-context-v2-candidate" ||
            count.decimalValue().compareTo(BigDecimal.valueOf(history.size.toLong())) != 0 ||
            window.stringValue() != transaction.transactionDate.minusDays(90).format(DATE_FORMAT)) return null
        val hasGraphHistory = graphContext.any { (name, value) -> name.endsWith("Count") && (value as Number).toLong() > 0 }
        val expectedContext = if (priorSummary.senderCount == 0L && !hasGraphHistory) "cold_start" else "history_provided"
        if (context.stringValue() != expectedContext ||
            evidenceType.stringValue() != "observed transaction history; not a causal explanation") return null
        val observedValues = observed.properties().toList()
        if (observedValues.map { it.key }.toSet() != OBSERVED_CONTEXT_FIELDS ||
            observedValues.any { !it.value.isNumber || !it.value.doubleValue().isFinite() || it.value.doubleValue() < 0 }) return null
        // The explanation shown to a reviewer must agree with the server's
        // recorded history, independently of the model's response.
        val expectedObserved = observedContext(transaction, history, priorSummary)
        if (observedValues.any { (name, value) ->
                val expected = expectedObserved.getValue(name)
                abs(value.doubleValue() - expected) > max(1.0, abs(expected)) * 1e-8
            }) return null
        val values = snapshot.properties().toList()
        if (values.map { it.key }.toSet() != FEATURE_FIELDS ||
            values.any { !it.value.isNumber || !it.value.doubleValue().isFinite() }) return null
        return observedValues.associate { it.key to it.value.doubleValue() } to
            values.associate { it.key to it.value.doubleValue() }
    }

    private fun observedContext(
        transaction: BankContextualTransaction, history: List<BankContextualTransaction>, summary: BankPriorSummary,
    ): Map<String, Double> {
        val recent7 = history.filter { !it.transactionDate.isBefore(transaction.transactionDate.minusDays(7)) }
        val recent30 = history.filter { !it.transactionDate.isBefore(transaction.transactionDate.minusDays(30)) }
        val sameRecipient = history.filter { it.destinationAccount == transaction.destinationAccount && it.destinationInstitution == transaction.destinationInstitution }
        fun mean(rows: List<BankContextualTransaction>): Double =
            if (rows.isEmpty()) 0.0 else rows.sumOf { it.amount }.toDouble() / rows.size
        fun age(rows: List<BankContextualTransaction>): Double =
            rows.maxOfOrNull { it.transactionDate }?.let { ChronoUnit.DAYS.between(it, transaction.transactionDate).toDouble() } ?: 91.0
        return mapOf(
            "sender7dCount" to recent7.size.toDouble(),
            "sender30dCount" to recent30.size.toDouble(),
            "sender90dCount" to history.size.toDouble(),
            "sender30dMeanAmount" to mean(recent30),
            "sender90dMeanAmount" to mean(history),
            "sender90dMaxAmount" to (history.maxOfOrNull { it.amount }?.toDouble() ?: 0.0),
            "distinctRecipients90d" to history.map { it.destinationInstitution to it.destinationAccount }.toSet().size.toDouble(),
            "recipient90dCount" to sameRecipient.size.toDouble(),
            "recipientBank90dCount" to history.count { it.destinationInstitution == transaction.destinationInstitution }.toDouble(),
            "senderLifetimeCount" to summary.senderCount.toDouble(),
            "senderLifetimeMeanAmount" to (if (summary.senderCount == 0L) 0.0 else summary.senderAmountSum.toDouble() / summary.senderCount),
            "senderLifetimeMaxAmount" to summary.senderAmountMax.toDouble(),
            "distinctRecipientsLifetime" to summary.distinctRecipients.toDouble(),
            "recipientLifetimeCount" to summary.recipientCount.toDouble(),
            "daysSinceLastSender" to age(history),
            "daysSinceLastRecipient" to age(sameRecipient),
        )
    }

    private fun validGraphResponse(node: JsonNode?, expected: Map<String, Any>): Boolean {
        if (node?.isObject != true || node.properties().map { it.key }.toSet() != expected.keys) return false
        return expected.all { (name, value) ->
            val actual = node[name]
            when {
                name == "asOfDate" -> actual.isString && actual.stringValue() == value
                name.endsWith("Amount") -> actual.isNumber && actual.doubleValue().isFinite() &&
                    abs(actual.doubleValue() - (value as Number).toDouble()) <= max(1.0, abs(value.toDouble())) * 1e-8
                else -> actual.isIntegralNumber && actual.longValue() == (value as Number).toLong()
            }
        }
    }

    private fun replay(key: String, hash: String): ResponseEntity<String>? {
        val stored = decisions.findByRequestKey(key) ?: return null
        if (stored.requestHash != hash) return error(HttpStatus.CONFLICT, "Idempotency-Key was used with a different request")
        val evidence = contextual.findEvidence(stored.decision.id, mapper)
            ?: return error(HttpStatus.CONFLICT, "Idempotency-Key belongs to a different bank API")
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(mapper.writeValueAsString(evidence))
    }

    private fun error(status: HttpStatus, message: String): ResponseEntity<String> =
        ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON)
            .body(mapper.writeValueAsString(mapOf("error" to message)))

    private companion object {
        val TRANSACTION_FIELDS = setOf(
            "출금계좌일련번호", "입금계좌일련번호", "출금금융회사일련번호", "입금금융회사일련번호",
            "자금구분", "거래금액", "거래시간대", "매체구분", "거래일자",
        )
        val IDENTIFIER = Regex("[A-Za-z0-9._~-]{1,64}")
        val HOUR_CODES = (0..21 step 3).map(Int::toDouble).toSet()
        val FUND_TYPES = setOf("0", "1", "3", "4")
        val CHANNELS = (1..7).map(Int::toString).toSet()
        val OBSERVED_CONTEXT_FIELDS = setOf(
            "sender7dCount", "sender30dCount", "sender90dCount", "sender30dMeanAmount",
            "sender90dMeanAmount", "sender90dMaxAmount", "distinctRecipients90d",
            "recipient90dCount", "recipientBank90dCount", "senderLifetimeCount",
            "senderLifetimeMeanAmount", "senderLifetimeMaxAmount", "distinctRecipientsLifetime",
            "recipientLifetimeCount", "daysSinceLastSender", "daysSinceLastRecipient",
        )
        val FEATURE_FIELDS = setOf(
            "amount_log", "time_bucket", "fund_code", "channel_code", "same_institution", "day_of_week",
            "sender_7d_count_log", "sender_30d_count_log", "sender_90d_count_log",
            "sender_30d_mean_amount_log", "sender_90d_mean_amount_log", "sender_90d_max_amount_log",
            "amount_to_30d_mean", "amount_to_90d_mean", "amount_to_90d_max",
            "sender_90d_distinct_recipients_log", "recipient_90d_count_log", "recipient_bank_90d_count_log",
            "same_channel_90d_share", "same_fund_90d_share", "same_time_90d_share", "days_since_sender", "days_since_recipient",
            "sender_all_count_log", "sender_all_mean_amount_log", "sender_all_max_amount_log", "amount_to_all_mean", "amount_to_all_max",
            "sender_all_distinct_recipients_log", "recipient_all_count_log", "recipient_bank_all_count_log",
            "same_channel_all_share", "same_fund_all_share", "days_since_first_sender", "days_since_last_sender_all", "days_since_last_recipient_all",
        ) + setOf(
            "senderDayCount", "senderDayAmount", "senderDayDistinctRecipients", "senderPairDayCount",
            "sender24hCount", "sender24hAmount", "senderInflow7dCount", "senderInflow30dCount",
            "senderInflow90dCount", "senderInflow90dAmount", "senderInflow90dDistinctSources", "senderDayInflowCount", "senderDayInflowAmount",
            "recipientInflow7dCount", "recipientInflow30dCount", "recipientInflow90dCount", "recipientInflow90dAmount",
            "recipientInflow90dDistinctSources", "recipientDayInflowCount", "recipientDayInflowAmount",
            "recipientOutflow90dCount", "recipientOutflow90dAmount", "recipientDayOutflowCount", "recipientDayOutflowAmount",
        ).map { "graph_${it}_log" } + setOf(
            "graph_amount_to_sender_day_mean", "graph_amount_to_sender_inflow_90d_mean", "graph_amount_to_sender_day_inflow",
            "graph_recipient_net_90d_signed_log", "graph_sender_net_day_signed_log", "graph_sender_pair_day_share",
        )
        val MAX_AMOUNT = BigDecimal("100000000000000000")
        val DATE_FORMAT: DateTimeFormatter = DateTimeFormatter.ofPattern("uuuuMMdd").withResolverStyle(ResolverStyle.STRICT)
        const val MAX_HISTORY_ROWS = 20_000
        const val MAX_MODEL_BODY_BYTES = 4 * 1024 * 1024
        val graphLock = BankGraphSynchronization.lock
    }
}
