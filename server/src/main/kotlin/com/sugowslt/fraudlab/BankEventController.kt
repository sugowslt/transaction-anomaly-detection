package com.sugowslt.fraudlab

import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.time.Duration
import org.springframework.beans.factory.annotation.Value
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import org.springframework.web.server.ResponseStatusException
import tools.jackson.databind.ObjectMapper

@RestController
@RequestMapping("/api/bank")
class BankEventController(
    @Value("\${fraud.model-url}") private val modelUrl: String,
    private val repository: BankDecisionRepository,
    private val objectMapper: ObjectMapper,
) {
    private val client = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(2))
        .build()

    @PostMapping("/events", consumes = [MediaType.APPLICATION_JSON_VALUE], produces = [MediaType.APPLICATION_JSON_VALUE])
    fun scoreAndStore(@RequestBody transaction: String): ResponseEntity<String> {
        if (transaction.toByteArray(StandardCharsets.UTF_8).size !in 1..65_536) {
            return ResponseEntity.badRequest().contentType(MediaType.APPLICATION_JSON)
                .body("{\"error\":\"Request body size must be 1-65536 bytes\"}")
        }
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
            return ResponseEntity.status(response.statusCode()).contentType(MediaType.APPLICATION_JSON).body(response.body())
        }
        val input = objectMapper.readTree(transaction)
        val score = objectMapper.readTree(response.body())
        val decision = repository.save(
            amount = input["거래금액"].decimalValue(),
            timeBucket = input["거래시간대"].intValue(),
            fundType = input["자금구분"].stringValue(),
            channel = input["매체구분"].stringValue(),
            riskScore = score["riskScore"].doubleValue(),
            alert = score["alert"].booleanValue(),
            threshold = score["threshold"].doubleValue(),
            modelVersion = score["modelVersion"].stringValue(),
        )
        return ResponseEntity.status(HttpStatus.CREATED).contentType(MediaType.APPLICATION_JSON)
            .body(objectMapper.writeValueAsString(decision))
    }

    @GetMapping("/alerts", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun alerts(@RequestParam(defaultValue = "20") limit: Int): List<BankDecision> {
        if (limit !in 1..100) {
            throw ResponseStatusException(HttpStatus.BAD_REQUEST, "limit must be between 1 and 100")
        }
        return repository.findAlerts(limit)
    }

    private fun unavailable(): ResponseEntity<String> =
        ResponseEntity.status(503).contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Model service unavailable\"}")
}
