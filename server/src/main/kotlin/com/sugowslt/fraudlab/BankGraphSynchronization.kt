package com.sugowslt.fraudlab

import java.util.concurrent.locks.ReentrantLock

/** One local process serializes raw writes and immutable window closure. */
object BankGraphSynchronization {
    val lock = ReentrantLock()
}
