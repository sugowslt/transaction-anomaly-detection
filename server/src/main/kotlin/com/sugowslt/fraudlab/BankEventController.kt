package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.Duration
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException
import java.util.HexFormat
import java.util.UUID
import org.springframework.beans.factory.annotation.Value
import org.springframework.dao.DataIntegrityViolationException
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestHeader
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import org.springframework.web.server.ResponseStatusException
import tools.jackson.core.JacksonException
import tools.jackson.databind.JsonNode
import tools.jackson.databind.ObjectMapper
import tools.jackson.databind.cfg.JsonNodeFeature

data class BankDecisionPage(
    val items: List<BankDecision>,
    val nextCursor: BankDecisionCursor?,
)

@RestController
@RequestMapping("/api/bank")
class BankEventController(
    @Value("\${fraud.model-url}") private val modelUrl: String,
    private val repository: BankDecisionRepository,
    private val monitoringService: BankMonitoringService,
    private val objectMapper: ObjectMapper,
) {
    private val bankFields = setOf("거래금액", "거래시간대", "자금구분", "매체구분")
    private val hourCodes = (0..21 step 3).map(Int::toDouble).toSet()
    private val fundTypes = setOf("0", "1", "3", "4")
    private val channels = (1..7).map(Int::toString).toSet()
    private val maximumAmount = BigDecimal("100000000000000000")
    private val client = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(2))
        .build()
    private val bankInputReader = objectMapper.reader().with(JsonNodeFeature.USE_BIG_DECIMAL_FOR_FLOATS)

    @PostMapping("/events", consumes = [MediaType.APPLICATION_JSON_VALUE], produces = [MediaType.APPLICATION_JSON_VALUE])
    fun scoreAndStore(
        @RequestBody transaction: String,
        @RequestHeader(value = "Idempotency-Key", required = false) idempotencyKey: String? = null,
    ): ResponseEntity<String> {
        if (transaction.toByteArray(StandardCharsets.UTF_8).size !in 1..65_536) {
            return ResponseEntity.badRequest().contentType(MediaType.APPLICATION_JSON)
                .body("{\"error\":\"Request body size must be 1-65536 bytes\"}")
        }
        if (idempotencyKey != null && !idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{1,64}"))) {
            return ResponseEntity.badRequest().contentType(MediaType.APPLICATION_JSON)
                .body("{\"error\":\"Invalid Idempotency-Key\"}")
        }
        val requestHash = HexFormat.of().formatHex(
            MessageDigest.getInstance("SHA-256").digest(transaction.toByteArray(StandardCharsets.UTF_8)),
        )
        if (idempotencyKey != null) {
            repository.findByRequestKey(idempotencyKey)?.let { return replay(it, requestHash) }
        }
        val input = try {
            bankInputReader.readTree(transaction)
        } catch (exception: JacksonException) {
            return invalidInput()
        }
        if (!validInput(input)) return invalidInput()
        val request = HttpRequest.newBuilder(URI.create(modelUrl.trimEnd('/') + "/score/bank"))
            .timeout(Duration.ofSeconds(4))
            .header("Content-Type", MediaType.APPLICATION_JSON_VALUE)
            .POST(HttpRequest.BodyPublishers.ofString(transaction))
            .build()
        val response = try {
            client.send(request, HttpResponse.BodyHandlers.ofString())
        } catch (exception: InterruptedException) {
            Thread.currentThread().interrupt()
            return unavailable()
        } catch (exception: java.io.IOException) {
            return unavailable()
        }
        if (response.statusCode() !in 200..299) {
            return modelServiceFailed()
        }
        val score = try {
            objectMapper.readTree(response.body())
        } catch (exception: JacksonException) {
            return invalidModelResponse()
        }
        if (!validModelResponse(score)) return invalidModelResponse()
        val decision = try {
            repository.save(
                amount = input["거래금액"].decimalValue(),
                timeBucket = input["거래시간대"].intValue(),
                fundType = input["자금구분"].stringValue(),
                channel = input["매체구분"].stringValue(),
                riskScore = score["riskScore"].doubleValue(),
                alert = score["alert"].booleanValue(),
                threshold = score["threshold"].doubleValue(),
                modelVersion = score["modelVersion"].stringValue(),
                requestKey = idempotencyKey,
                requestHash = if (idempotencyKey != null) requestHash else null,
            )
        } catch (exception: DataIntegrityViolationException) {
            if (idempotencyKey != null) {
                repository.findByRequestKey(idempotencyKey)?.let { return replay(it, requestHash) }
            }
            throw exception
        }
        val storedDecision = idempotencyKey?.let { repository.findByRequestKey(it)?.decision } ?: decision
        return decisionResponse(storedDecision, HttpStatus.CREATED)
    }

    @GetMapping("/alerts", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun alerts(@RequestParam(defaultValue = "20") limit: Int): List<BankDecision> {
        if (limit !in 1..100) {
            throw ResponseStatusException(HttpStatus.BAD_REQUEST, "limit must be between 1 and 100")
        }
        return repository.findAlerts(limit)
    }

    @GetMapping("/decisions", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun decisions(
        @RequestParam(defaultValue = "20") limit: Int,
        @RequestParam(required = false) alert: Boolean? = null,
        @RequestParam(required = false) beforeCreatedAt: String? = null,
        @RequestParam(required = false) beforeId: String? = null,
    ): BankDecisionPage {
        if (limit !in 1..100 || (beforeCreatedAt == null) != (beforeId == null)) {
            throw ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid decision page parameters")
        }
        val cursor = if (beforeCreatedAt == null) null else {
            val createdAt = try {
                OffsetDateTime.parse(beforeCreatedAt)
            } catch (exception: DateTimeParseException) {
                throw ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid decision cursor")
            }
            val id = try {
                UUID.fromString(beforeId).toString()
            } catch (exception: IllegalArgumentException) {
                throw ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid decision cursor")
            }
            BankDecisionCursor(createdAt, id)
        }
        val rows = repository.findDecisions(limit + 1, alert, cursor)
        val items = rows.take(limit)
        val next = if (rows.size > limit) items.last().let { BankDecisionCursor(it.createdAt, it.id) } else null
        return BankDecisionPage(items, next)
    }

    @GetMapping("/monitoring", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun monitoring(): BankMonitoringSnapshot = monitoringService.snapshot()

    private fun validInput(input: JsonNode?): Boolean {
        if (input == null || !input.isObject || input.properties().map { it.key }.toSet() != bankFields) return false
        val amount = input["거래금액"]
        val hour = input["거래시간대"]
        val fundType = input["자금구분"]
        val channel = input["매체구분"]
        if (amount?.isNumber != true || hour?.isNumber != true) return false
        val amountValue = amount.decimalValue()
        return amountValue >= BigDecimal.ZERO && amountValue < maximumAmount &&
            amountValue.toDouble() < maximumAmount.toDouble() &&
            amountValue.stripTrailingZeros().scale() <= 2 &&
            hour.doubleValue() in hourCodes && fundType?.isString == true && fundType.stringValue() in fundTypes &&
            channel?.isString == true && channel.stringValue() in channels
    }

    private fun validModelResponse(score: JsonNode?): Boolean {
        if (score == null || !score.isObject) return false
        val risk = score["riskScore"]
        val alert = score["alert"]
        val threshold = score["threshold"]
        val version = score["modelVersion"]
        if (risk?.isNumber != true || alert?.isBoolean != true || threshold?.isNumber != true || version?.isString != true) {
            return false
        }
        return risk.doubleValue().isFinite() && risk.doubleValue() in 0.0..1.0 &&
            threshold.doubleValue().isFinite() && threshold.doubleValue() in 0.0..1.0 &&
            alert.booleanValue() == (risk.doubleValue() >= threshold.doubleValue()) &&
            version.stringValue().isNotBlank() && version.stringValue().length <= 64
    }

    private fun invalidInput(): ResponseEntity<String> =
        ResponseEntity.badRequest().contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Invalid bank transaction\"}")

    private fun invalidModelResponse(): ResponseEntity<String> =
        ResponseEntity.status(HttpStatus.BAD_GATEWAY).contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Invalid model response\"}")

    private fun modelServiceFailed(): ResponseEntity<String> =
        ResponseEntity.status(HttpStatus.BAD_GATEWAY).contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Model service failed\"}")

    private fun replay(stored: BankStoredRequest, requestHash: String): ResponseEntity<String> =
        if (stored.requestHash == requestHash) decisionResponse(stored.decision, HttpStatus.OK)
        else ResponseEntity.status(HttpStatus.CONFLICT).contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Idempotency-Key was used with a different request\"}")

    private fun decisionResponse(decision: BankDecision, status: HttpStatus): ResponseEntity<String> =
        ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON)
            .body(objectMapper.writeValueAsString(decision))

    private fun unavailable(): ResponseEntity<String> =
        ResponseEntity.status(503).contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Model service unavailable\"}")
}
