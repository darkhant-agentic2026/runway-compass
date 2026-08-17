/**
 * Fractional index ordering — a port of `apps/api/src/coach/services/ordering.py`.
 *
 * docs/06-frontend.md:
 *
 * > Reorder patches the fractional index locally using the same midpoint algorithm as
 * > the server, so the optimistic order matches the confirmed order.
 *
 * The two implementations must agree exactly; `src/lib/ordering.test.ts` pins the shared
 * vectors and docs/08-testing.md asserts "the optimistic fractional index equals the
 * server's". **Change one, change both.**
 *
 * Algorithm after the `fractional-indexing` reference implementation
 * (https://github.com/rocicorp/fractional-indexing, MIT).
 */

const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

/** The key handed out when a list is empty. */
export const FIRST_KEY = 'a0'

const SMALLEST_INTEGER = 'A' + '0'.repeat(26)

/** A malformed order key, or an ordering that cannot be represented. */
export class OrderKeyError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'OrderKeyError'
  }
}

/**
 * No key can be generated in the requested position — only reachable at the extreme ends
 * of the key space. The caller rebalances rather than surfacing this.
 */
export class OrderKeyExhausted extends OrderKeyError {
  constructor(message: string) {
    super(message)
    this.name = 'OrderKeyExhausted'
  }
}

function integerLength(head: string): number {
  if (head >= 'a' && head <= 'z') return head.charCodeAt(0) - 'a'.charCodeAt(0) + 2
  if (head >= 'A' && head <= 'Z') return 'Z'.charCodeAt(0) - head.charCodeAt(0) + 2
  throw new OrderKeyError(`invalid order key head: ${head}`)
}

function integerPart(key: string): string {
  if (key.length === 0) throw new OrderKeyError('empty order key')
  const length = integerLength(key[0]!)
  if (length > key.length) throw new OrderKeyError(`invalid order key: ${key}`)
  return key.slice(0, length)
}

/** Throws unless `key` is a well-formed order key. */
export function validateKey(key: string): void {
  if (key === SMALLEST_INTEGER) throw new OrderKeyError(`invalid order key: ${key}`)
  const integer = integerPart(key)
  const fraction = key.slice(integer.length)
  if (fraction.endsWith(BASE62[0]!)) {
    throw new OrderKeyError(`invalid order key (trailing zero): ${key}`)
  }
}

function validateInteger(value: string): void {
  if (value.length !== integerLength(value[0]!)) {
    throw new OrderKeyError(`invalid integer part: ${value}`)
  }
}

function midpoint(a: string, b: string | null): string {
  if (b !== null && a >= b) throw new OrderKeyError(`${a} >= ${b}`)
  if (a.endsWith('0') || (b !== null && b.endsWith('0'))) {
    throw new OrderKeyError('trailing zero in fraction')
  }

  if (b !== null) {
    let n = 0
    while (n < b.length && (n < a.length ? a[n] : '0') === b[n]) n += 1
    if (n > 0) return b.slice(0, n) + midpoint(a.slice(n), b.slice(n))
  }

  const digitA = a.length > 0 ? BASE62.indexOf(a[0]!) : 0
  const digitB = b !== null ? BASE62.indexOf(b[0]!) : BASE62.length
  if (digitB - digitA > 1) {
    return BASE62[Math.round(0.5 * (digitA + digitB))]!
  }
  if (b !== null && b.length > 1) return b.slice(0, 1)
  return BASE62[digitA]! + midpoint(a.slice(1), null)
}

function incrementInteger(value: string): string | null {
  validateInteger(value)
  const head = value[0]!
  const digits = value.slice(1).split('')
  let carry = true
  for (let i = digits.length - 1; carry && i >= 0; i -= 1) {
    const d = BASE62.indexOf(digits[i]!) + 1
    if (d === BASE62.length) {
      digits[i] = BASE62[0]!
    } else {
      digits[i] = BASE62[d]!
      carry = false
    }
  }
  if (carry) {
    if (head === 'Z') return 'a' + BASE62[0]!
    if (head === 'z') return null
    const h = String.fromCharCode(head.charCodeAt(0) + 1)
    if (h > 'a') digits.push(BASE62[0]!)
    else digits.pop()
    return h + digits.join('')
  }
  return head + digits.join('')
}

function decrementInteger(value: string): string | null {
  validateInteger(value)
  const head = value[0]!
  const digits = value.slice(1).split('')
  let borrow = true
  for (let i = digits.length - 1; borrow && i >= 0; i -= 1) {
    const d = BASE62.indexOf(digits[i]!) - 1
    if (d === -1) {
      digits[i] = BASE62[BASE62.length - 1]!
    } else {
      digits[i] = BASE62[d]!
      borrow = false
    }
  }
  if (borrow) {
    if (head === 'a') return 'Z' + BASE62[BASE62.length - 1]!
    if (head === 'A') return null
    const h = String.fromCharCode(head.charCodeAt(0) - 1)
    if (h < 'Z') digits.push(BASE62[BASE62.length - 1]!)
    else digits.pop()
    return h + digits.join('')
  }
  return head + digits.join('')
}

/**
 * A key that sorts strictly after `a` and strictly before `b`.
 *
 * `null` means unbounded on that side: `keyBetween(null, null)` is the first key,
 * `keyBetween(last, null)` appends, `keyBetween(null, first)` prepends.
 */
export function keyBetween(a: string | null, b: string | null): string {
  if (a !== null) validateKey(a)
  if (b !== null) validateKey(b)
  if (a !== null && b !== null && a >= b) {
    throw new OrderKeyError(`order keys out of sequence: ${a} >= ${b}`)
  }

  if (a === null) {
    if (b === null) return FIRST_KEY
    const integerB = integerPart(b)
    const fractionB = b.slice(integerB.length)
    if (integerB === SMALLEST_INTEGER) return integerB + midpoint('', fractionB)
    if (integerB < b) return integerB
    const decremented = decrementInteger(integerB)
    if (decremented === null) {
      throw new OrderKeyExhausted('no room below the smallest order key')
    }
    return decremented
  }

  if (b === null) {
    const integerA = integerPart(a)
    const fractionA = a.slice(integerA.length)
    const incremented = incrementInteger(integerA)
    if (incremented === null) return integerA + midpoint(fractionA, null)
    return incremented
  }

  const integerA = integerPart(a)
  const fractionA = a.slice(integerA.length)
  const integerB = integerPart(b)
  const fractionB = b.slice(integerB.length)
  if (integerA === integerB) return integerA + midpoint(fractionA, fractionB)
  const incremented = incrementInteger(integerA)
  if (incremented === null) throw new OrderKeyExhausted('no room above the largest order key')
  if (incremented < b) return incremented
  return integerA + midpoint(fractionA, null)
}

/** `n` keys, ascending, strictly between `a` and `b`. */
export function nKeysBetween(a: string | null, b: string | null, n: number): string[] {
  if (n < 0) throw new Error('n must be non-negative')
  if (n === 0) return []
  if (n === 1) return [keyBetween(a, b)]

  if (b === null) {
    let key = keyBetween(a, b)
    const keys = [key]
    for (let i = 0; i < n - 1; i += 1) {
      key = keyBetween(key, b)
      keys.push(key)
    }
    return keys
  }

  if (a === null) {
    let key = keyBetween(a, b)
    const keys = [key]
    for (let i = 0; i < n - 1; i += 1) {
      key = keyBetween(a, key)
      keys.push(key)
    }
    keys.reverse()
    return keys
  }

  const mid = Math.floor(n / 2)
  const key = keyBetween(a, b)
  return [...nKeysBetween(a, key, mid), key, ...nKeysBetween(key, b, n - mid - 1)]
}

/**
 * The order key a task would get by moving to `targetIndex` in `siblings`.
 *
 * This is the client half of `plan_move` in the Python service: the board uses it during
 * a drag so the row lands in its final position immediately, and the server recomputes
 * the same value from the same neighbours. `siblings` must be sorted by `order` and must
 * still contain the task being moved.
 */
export function orderForMove(
  siblings: readonly { id: string; order: string }[],
  movingId: string,
  targetIndex: number,
): string {
  const others = siblings.filter((task) => task.id !== movingId)
  const clamped = Math.max(0, Math.min(targetIndex, others.length))
  const previous = clamped > 0 ? others[clamped - 1]!.order : null
  const following = clamped < others.length ? others[clamped]!.order : null
  return keyBetween(previous, following)
}
