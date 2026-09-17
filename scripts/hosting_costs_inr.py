"""INR costs for a 15-user, 4-week round. Arithmetic in a script, not in my head."""

RATE = 95.68          # USD -> INR, 11 Sep 2026
FOREX_MARKUP = 0.035  # typical Indian credit-card cross-currency fee
GST = 0.18

WEEKS = 4
DAYS = 28
HOURS_PER_DAY = 10          # study hours, matching freeze item 7
GPU_HOURS = DAYS * HOURS_PER_DAY


def inr(usd):
    return usd * RATE


def landed(usd):
    """What actually leaves an Indian card: price + forex markup, then GST on top."""
    base = inr(usd) * (1 + FOREX_MARKUP)
    return base, base * (1 + GST)


print('USD->INR %.2f | forex markup %.1f%% | GST %d%%' % (RATE, FOREX_MARKUP * 100, GST * 100))
print('GPU usage assumed: %d h/day x %d days = %d hours\n' % (HOURS_PER_DAY, DAYS, GPU_HOURS))

print('MONTHLY VPS (CPU only)')
print('%-26s %10s %12s %14s' % ('plan', 'USD/mo', 'INR/mo', 'INR landed'))
for name, usd in [
    ('Contabo 4vCPU/8GB', 6.99),
    ('Hostinger KVM 8GB', 6.49),
    ('Hetzner CPX31 (now)', 17.99),
    ('Hetzner CPX31 (new)', 24.99),
    ('DigitalOcean 8GB', 48.00),
]:
    base, with_gst = landed(usd)
    print('%-26s %10.2f %12s %14s' % (name, usd, '%.0f' % inr(usd), '%.0f' % with_gst))

print('\nFOUR-WEEK TOTAL, CPU only (one month of the above)')
for name, usd in [('Contabo', 6.99), ('Hetzner CPX31', 17.99)]:
    print('  %-22s ~%s landed' % (name, ('%.0f' % landed(usd)[1])))

print('\nGPU BY THE HOUR, %d hours' % GPU_HOURS)
print('%-26s %10s %14s %14s' % ('provider', 'per hour', 'INR total', 'INR landed'))
for name, usd_hr, native_inr in [
    ('Vast.ai (from)', 0.03, None),
    ('RunPod (from)', 0.24, None),
    ('E2E Networks (from)', None, 49.0),
]:
    if native_inr is not None:
        total = native_inr * GPU_HOURS
        # Indian provider: no forex, GST on the invoice and reclaimable if registered.
        print('%-26s %10s %14s %14s' % (name, '%.0f INR' % native_inr,
                                        '%.0f' % total, '%.0f' % (total * (1 + GST))))
    else:
        total_usd = usd_hr * GPU_HOURS
        print('%-26s %10s %14s %14s' % (name, '$%.2f' % usd_hr,
                                        '%.0f' % inr(total_usd), '%.0f' % landed(total_usd)[1]))

print('\nFREE CREDITS, in INR')
for name, usd, window in [
    ('Google Cloud', 300, '90 days'),
    ('AWS', 200, '6 months'),
    ('Azure', 200, '30 days'),
]:
    print('  %-14s $%-5d = ~%8s   (%s)' % (name, usd, '%.0f' % inr(usd), window))

print('\nSanity: a GPU VM at $0.50/hr for the whole round')
half = 0.50 * GPU_HOURS
print('  $%.2f = ~%.0f INR  -> inside the GCP credit? %s'
      % (half, inr(half), 'yes' if half < 300 else 'NO'))
