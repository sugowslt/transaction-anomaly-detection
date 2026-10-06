package com.sugowslt.fraudlab

import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeParseException
import kotlin.concurrent.withLock
import kotlin.math.abs
import org.springframework.beans.factory.annotation.Value
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.web.bind.annotation.*
import tools.jackson.core.JacksonException
import tools.jackson.databind.ObjectMapper

/** Delayed review of server-owned, closed observation buckets, not a payment veto. */
@RestController
@RequestMapping("/api/bank")
class BankClosedWindowController(
    @Value("\${fraud.model-url}") private val modelUrl: String,
    private val jdbc: JdbcTemplate,
    private val mapper: ObjectMapper,
    @Value("\${fraud.bank-v2-candidate-enabled:false}") private val enabled: Boolean = false,
    @Value("\${fraud.reports-dir:../reports}") private val reportsDir: String = "../reports",
) {
    private val client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build()
    private data class StoredEvent(val id: String, val transaction: BankContextualTransaction, val snapshot: Map<String, Double>, val contextStatus: String)

    @GetMapping("/closed-window-metrics", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun metrics(): ResponseEntity<String> {
        val file = activeReport()
        return if (Files.isRegularFile(file)) json(HttpStatus.OK, Files.readString(file))
        else error(HttpStatus.NOT_FOUND, "Closed-window model report is unavailable")
    }

    @GetMapping("/closed-window-reviews", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun lookup(@RequestParam date: String, @RequestParam timeBucket: Int): ResponseEntity<String> {
        val day = parseDate(date) ?: return error(HttpStatus.BAD_REQUEST, "Invalid YYYYMMDD date")
        if (timeBucket !in 0..21 || timeBucket % 3 != 0) return error(HttpStatus.BAD_REQUEST, "Invalid three-hour bucket")
        return stored(day, timeBucket)?.let { json(HttpStatus.OK, it) }
            ?: error(HttpStatus.NOT_FOUND, "This observation window has not been reviewed")
    }

    @PostMapping("/closed-window-reviews", consumes = [MediaType.APPLICATION_JSON_VALUE], produces = [MediaType.APPLICATION_JSON_VALUE])
    fun review(@RequestBody body: String): ResponseEntity<String> {
        if (!enabled) return error(HttpStatus.SERVICE_UNAVAILABLE, "Research review path is disabled")
        if (body.toByteArray(StandardCharsets.UTF_8).size !in 1..4096) return error(HttpStatus.BAD_REQUEST, "Invalid request size")
        val input = try { mapper.readTree(body) } catch (_: JacksonException) { return error(HttpStatus.BAD_REQUEST, "Invalid JSON") }
        if (!input.isObject || input.properties().map { it.key }.toSet() != setOf("date", "timeBucket") ||
            input["date"]?.isString != true || input["timeBucket"]?.isIntegralNumber != true || !input["timeBucket"].canConvertToInt()) {
            return error(HttpStatus.BAD_REQUEST, "Only date and timeBucket are accepted; raw events and labels must come from the server")
        }
        val day = parseDate(input["date"].stringValue()) ?: return error(HttpStatus.BAD_REQUEST, "Invalid YYYYMMDD date")
        val hour = input["timeBucket"].intValue()
        if (hour !in 0..21 || hour % 3 != 0) return error(HttpStatus.BAD_REQUEST, "Invalid three-hour bucket")
        return BankGraphSynchronization.lock.withLock {
            stored(day, hour)?.let { return@withLock json(HttpStatus.OK, it) }
            if (day.atTime(hour, 0).plusHours(3).isAfter(LocalDateTime.now(ZoneId.of("Asia/Seoul")))) {
                return@withLock error(HttpStatus.CONFLICT, "The observation period has not ended")
            }
            val rows = jdbc.query(
                """SELECT e.*,d.amount,d.time_bucket,d.channel,d.fund_type FROM bank_graph_event e
                   JOIN bank_decision d ON d.id=e.decision_id
                   WHERE e.transaction_date=? AND e.transaction_hour=? ORDER BY e.decision_id LIMIT 20001""".trimIndent(),
                RowMapper { row, _ ->
                    val snapshot = mapper.readTree(row.getString("feature_snapshot"))
                    StoredEvent(row.getString("decision_id"), BankContextualTransaction(
                        row.getString("source_account"), row.getString("destination_account"), row.getString("source_institution"),
                        row.getString("destination_institution"), row.getString("fund_type"), row.getBigDecimal("amount"),
                        row.getInt("time_bucket"), row.getString("channel"), row.getObject("transaction_date", LocalDate::class.java),
                    ), snapshot.properties().associate { it.key to it.value.doubleValue() }, row.getString("context_status"))
                }, day, hour,
            )
            if (rows.isEmpty()) return@withLock error(HttpStatus.NOT_FOUND, "No stored transactions in this window")
            if (rows.size > 20000) return@withLock error(HttpStatus.PAYLOAD_TOO_LARGE, "Observation window exceeds 20000 transactions; no rows were truncated")
            val payload = mapper.writeValueAsString(mapOf("events" to rows.map { it.transaction.modelInput() }, "priorSnapshots" to rows.map { it.snapshot }))
            if (payload.toByteArray(StandardCharsets.UTF_8).size > 16 * 1024 * 1024) return@withLock error(HttpStatus.PAYLOAD_TOO_LARGE, "Observation window exceeds model request size")
            val file = activeReport()
            if (!Files.isRegularFile(file)) return@withLock error(HttpStatus.SERVICE_UNAVAILABLE, "Closed-window model report is unavailable")
            val report = mapper.readTree(Files.readString(file))
            val request = HttpRequest.newBuilder(URI.create(modelUrl.trimEnd('/') + "/score/bank/window"))
                .timeout(Duration.ofSeconds(30)).header("Content-Type", MediaType.APPLICATION_JSON_VALUE)
                .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8)).build()
            val response = try { client.send(request, HttpResponse.BodyHandlers.ofInputStream()) }
            catch (_: java.io.IOException) { return@withLock error(HttpStatus.SERVICE_UNAVAILABLE, "Window model service unavailable") }
            catch (_: InterruptedException) { Thread.currentThread().interrupt(); return@withLock error(HttpStatus.SERVICE_UNAVAILABLE, "Window model request interrupted") }
            val responseBytes = try { response.body().use { it.readNBytes(8 * 1024 * 1024 + 1) } }
            catch (_: java.io.IOException) { return@withLock error(HttpStatus.BAD_GATEWAY, "Incomplete window model response") }
            if (response.statusCode() !in 200..299 || responseBytes.size > 8 * 1024 * 1024) {
                return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid window model response")
            }
            val result = try { mapper.readTree(responseBytes) } catch (_: JacksonException) { return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid window model JSON") }
            val threshold = result["threshold"]
            val hybrid = report["decision_policy"]?.stringValue() == "general_or_concurrent_specialist_v1"
            val expectedThreshold = if (hybrid) report["general_threshold"] else report["threshold_policy"]?.get("threshold")
            val specialistThreshold = result["specialistThreshold"]
            val hybridContractInvalid = if (hybrid) {
                result["decisionPolicy"]?.stringValue() != "general_or_concurrent_specialist_v1" ||
                    result["generalModelVersion"]?.stringValue() != report["general_model_version"]?.stringValue() ||
                    result["specialistModelVersion"]?.stringValue() != report["specialist_model_version"]?.stringValue() ||
                    specialistThreshold?.isNumber != true || !specialistThreshold.doubleValue().isFinite() ||
                    specialistThreshold.doubleValue() !in 0.5..1.0 ||
                    specialistThreshold.doubleValue() != report["specialist_threshold"]?.doubleValue()
            } else result["decisionPolicy"] != null || result["specialistThreshold"] != null
            if (result["modelVersion"]?.isString != true || result["modelVersion"].stringValue() != report["model_version"]?.stringValue() ||
                result["featureSchemaVersion"]?.stringValue() != "bank-closed-window-v1" ||
                result["observationMode"]?.stringValue() != "closed_three_hour_bucket_including_self_and_peers" ||
                threshold?.isNumber != true || !threshold.doubleValue().isFinite() || threshold.doubleValue() !in 0.0..1.0 ||
                threshold.doubleValue() != expectedThreshold?.doubleValue() || hybridContractInvalid ||
                result["items"]?.isArray != true || result["items"].size() != rows.size) {
                return@withLock error(HttpStatus.BAD_GATEWAY, "Window model version or contract mismatch")
            }
            val items = mutableListOf<Map<String, Any>>()
            val observations = BankWindowObservations.calculate(rows.map { it.transaction })
            for ((index, item) in result["items"].values().withIndex()) {
                val score = item["riskScore"]
                val snapshot = item["windowSnapshot"]
                val concurrentScore = item["concurrentScore"]
                val validHead = !hybrid || (concurrentScore?.isNumber == true && concurrentScore.doubleValue().isFinite() &&
                    concurrentScore.doubleValue() in 0.0..1.0 && item["generalAlert"]?.isBoolean == true &&
                    score?.isNumber == true && item["generalAlert"].booleanValue() == (score.doubleValue() >= threshold.doubleValue()) &&
                    item["concurrentAlert"]?.isBoolean == true &&
                    item["concurrentAlert"].booleanValue() == (concurrentScore.doubleValue() >= specialistThreshold.doubleValue()))
                val expectedAlert = if (hybrid && validHead) item["generalAlert"].booleanValue() || item["concurrentAlert"].booleanValue()
                    else score?.isNumber == true && score.doubleValue() >= threshold.doubleValue()
                if (item["index"]?.isIntegralNumber != true || !item["index"].canConvertToInt() || item["index"].intValue() != index ||
                    score?.isNumber != true || !score.doubleValue().isFinite() || score.doubleValue() !in 0.0..1.0 ||
                    !validHead || item["alert"]?.isBoolean != true || item["alert"].booleanValue() != expectedAlert ||
                    snapshot?.isObject != true || snapshot.properties().map { it.key }.toSet() != BankWindowObservations.fields.toSet() ||
                    snapshot.properties().any { !it.value.isNumber || !it.value.doubleValue().isFinite() || it.value.doubleValue() < 0 }) {
                    return@withLock error(HttpStatus.BAD_GATEWAY, "Invalid window score or observations")
                }
                val transaction = rows[index].transaction
                if (observations[index].any { (name, value) -> abs(snapshot[name].doubleValue() - value) > 1e-8 * maxOf(1.0, abs(value)) }) {
                    return@withLock error(HttpStatus.BAD_GATEWAY, "Window observations disagree with server ledger")
                }
                val savedItem = mutableMapOf<String, Any>("decisionId" to rows[index].id, "amount" to transaction.amount, "riskScore" to score.doubleValue(),
                    "alert" to item["alert"].booleanValue(), "windowSnapshot" to snapshot.properties().associate { it.key to it.value.doubleValue() },
                    "supportAssessment" to BankSupportAssessment.expected(transaction, rows[index].contextStatus))
                if (hybrid) savedItem.putAll(mapOf("generalAlert" to item["generalAlert"].booleanValue(),
                    "concurrentScore" to concurrentScore.doubleValue(), "concurrentAlert" to item["concurrentAlert"].booleanValue()))
                items += savedItem
            }
            val version = result["modelVersion"].stringValue()
            val now = OffsetDateTime.now()
            val savedReview = mutableMapOf<String, Any>("date" to day.toString(), "timeBucket" to hour, "observedRows" to rows.size,
                "reviewedAt" to now, "modelVersion" to version, "threshold" to threshold.doubleValue(), "alertCount" to items.count { it["alert"] == true },
                "observationMode" to "closed_three_hour_bucket_including_self_and_peers", "items" to items,
                "scope" to "Observed local window only; delayed synthetic-label research review, not confirmation of fraud")
            if (hybrid) savedReview.putAll(mapOf("generalModelVersion" to result["generalModelVersion"].stringValue(),
                "specialistModelVersion" to result["specialistModelVersion"].stringValue(), "specialistThreshold" to specialistThreshold.doubleValue(),
                "decisionPolicy" to "general_or_concurrent_specialist_v1"))
            val saved = mapper.writeValueAsString(savedReview)
            jdbc.update("INSERT INTO bank_closed_window_review(transaction_date,transaction_hour,reviewed_at,observed_rows,model_version,review_json) VALUES(?,?,?,?,?,?)",
                day, hour, now, rows.size, version, saved)
            json(HttpStatus.CREATED, saved)
        }
    }

    private fun activeReport(): Path = Path.of(reportsDir, "bank_closed_window_hybrid.json").takeIf { Files.isRegularFile(it) }
        ?: Path.of(reportsDir, "bank_closed_window.json")
    private fun stored(day: LocalDate, hour: Int): String? = jdbc.query("SELECT review_json FROM bank_closed_window_review WHERE transaction_date=? AND transaction_hour=?",
        RowMapper { row, _ -> row.getString(1) }, day, hour).singleOrNull()
    private fun parseDate(value: String): LocalDate? = try {
        if (!value.matches(Regex("[0-9]{8}"))) null else LocalDate.parse(value, java.time.format.DateTimeFormatter.BASIC_ISO_DATE)
    } catch (_: DateTimeParseException) { null }
    private fun json(status: HttpStatus, body: String) = ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON).body(body)
    private fun error(status: HttpStatus, message: String) = json(status, mapper.writeValueAsString(mapOf("error" to message)))

}
