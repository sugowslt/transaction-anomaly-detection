package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.time.LocalDate
import kotlin.math.ln1p
import kotlin.math.sqrt
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

class BankWindowObservationsTest {
    private fun event(source: String, destination: String, amount: String, institution: String = "10") =
        BankContextualTransaction(source, destination, institution, "10", "0", BigDecimal(amount), 9, "2", LocalDate.of(2024, 5, 2))

    @Test
    fun `closed observations count repeats reciprocal flow and institution namespaces independently of order`() {
        val rows = listOf(event("A", "B", "100"), event("A", "B", "100.00"), event("A", "C", "400"),
            event("B", "A", "50"), event("A", "B", "700", "20"))
        val output = BankWindowObservations.calculate(rows)
        val first = output[0]
        val std = sqrt(20000.0)
        val expected = listOf(ln1p(3.0), ln1p(600.0), ln1p(200.0), ln1p(400.0), ln1p(100.0), ln1p(std), std / 200,
            1.0 / 6, ln1p(2.0), ln1p(2.0), 2.0 / 3, ln1p(2.0), ln1p(200.0), 2.0 / 3,
            ln1p(3.0), ln1p(900.0), ln1p(300.0), ln1p(sqrt(80000.0)), ln1p(2.0), ln1p(1.0))
        BankWindowObservations.fields.zip(expected).forEach { (field, value) -> assertEquals(value, first[field]!!, 1e-10, field) }
        assertEquals(ln1p(1.0), output[4]["window_sender_count_log"]!!, 1e-10)
        assertEquals(output.reversed(), BankWindowObservations.calculate(rows.reversed()))
    }

    @Test
    fun `incomplete or mixed windows are rejected and zero totals remain finite`() {
        assertThrows(IllegalArgumentException::class.java) { BankWindowObservations.calculate(emptyList()) }
        assertThrows(IllegalArgumentException::class.java) { BankWindowObservations.calculate(List(20001) { event("A", "B", "1") }) }
        val zero = event("A", "B", "0")
        assertThrows(IllegalArgumentException::class.java) { BankWindowObservations.calculate(listOf(zero, zero.copy(timeBucket = 12))) }
        assertTrue(BankWindowObservations.calculate(listOf(zero)).single().values.all { it.isFinite() && it >= 0 })
    }
}
