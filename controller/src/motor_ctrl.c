/*
 * motor_ctrl.c - Motor speed controller with thermal derating.
 *
 * Each MC_Step() does, in order:
 *   1. Sanitise the speed request
 *   2. Plausibility-check both sensors and debounce faults
 *   3. Update the mode state machine  (SAFE > LIMP_HOME > DERATED > NORMAL)
 *   4. Compute and slew-limit the torque limit
 *   5. Run the PI speed controller (anti-windup) or ramp torque to zero in SAFE
 *   6. Publish outputs
 */
#include "motor_ctrl.h"

/* ---- Helpers ------------------------------------------------------------ */

static float clampf(float x, float lo, float hi)
{
    if (x < lo) { return lo; }
    if (x > hi) { return hi; }
    return x;
}

static float absf(float x)
{
    return (x < 0.0f) ? -x : x;
}

static float minf(float a, float b)
{
    return (a < b) ? a : b;
}

/* Move x towards target by at most max_delta */
static float approach(float x, float target, float max_delta)
{
    if (target > (x + max_delta)) { return x + max_delta; }
    if (target < (x - max_delta)) { return x - max_delta; }
    return target;
}

/* First-order low-pass, discretised with backward Euler */
static float lowpass(float y, float x, float dt, float tau)
{
    const float a = dt / (tau + dt);
    return y + (a * (x - y));
}

/* Count up while a condition is active, down while it is not, saturating */
static uint16_t debounce(uint16_t cnt, uint16_t active, uint16_t limit)
{
    if (active != 0u) {
        return (cnt < limit) ? (uint16_t)(cnt + 1u) : limit;
    }
    return (cnt > 0u) ? (uint16_t)(cnt - 1u) : (uint16_t)0u;
}

/* Fraction of torque_max allowed at a given winding temperature */
static float derate_factor(const MC_Cal *cal, float temp)
{
    float frac;
    if (temp <= cal->derate_start) {
        frac = 1.0f;
    } else if (temp >= cal->derate_end) {
        frac = cal->derate_min_frac;
    } else {
        const float x = (temp - cal->derate_start) / (cal->derate_end - cal->derate_start);
        frac = 1.0f - (x * (1.0f - cal->derate_min_frac));
    }
    return frac;
}

static void publish(const MC_Ctx *ctx, uint8_t derate_active, MC_Outputs *out)
{
    out->torque_cmd    = ctx->torque_cmd;
    out->torque_limit  = ctx->torque_limit;
    out->faults        = ctx->faults;
    out->mode          = (uint8_t)ctx->mode;
    out->derate_active = derate_active;
}

/* ---- Calibration -------------------------------------------------------- */

void MC_GetDefaultCal(MC_Cal *cal)
{
    cal->dt                   = 0.01f;
    cal->kp                   = 4.0f;
    cal->ki                   = 10.0f;
    cal->integ_limit          = 250.0f;
    cal->torque_max           = 250.0f;
    cal->derate_start         = 120.0f;
    cal->derate_end           = 150.0f;
    cal->derate_min_frac      = 0.2f;
    cal->derate_hyst          = 5.0f;
    cal->temp_critical        = 165.0f;
    cal->limp_frac            = 0.3f;
    cal->limit_slew_down      = 500.0f;
    cal->limit_slew_up        = 50.0f;
    cal->safe_ramp            = 1000.0f;
    cal->temp_min_valid       = -40.0f;
    cal->temp_max_valid       = 220.0f;
    cal->temp_jump            = 10.0f;
    cal->temp_filt_tau        = 0.5f;
    cal->speed_max_valid      = 1200.0f;
    cal->speed_jump           = 100.0f;
    cal->speed_filt_tau       = 0.05f;
    cal->fault_debounce_ticks = 5u;
}

/* Every check is written as "value is inside the good range", so a NaN
 * (for which every comparison is false) fails the check automatically. */
uint16_t MC_CheckCal(const MC_Cal *cal)
{
    int ok = 1;
    ok = ok && (cal->dt > 0.0f) && (cal->dt <= 0.1f);
    ok = ok && (cal->kp >= 0.0f) && (cal->kp <= 100.0f);
    ok = ok && (cal->ki >= 0.0f) && (cal->ki <= 1000.0f);
    ok = ok && (cal->torque_max > 0.0f) && (cal->torque_max <= 1000.0f);
    ok = ok && (cal->integ_limit > 0.0f) && (cal->integ_limit <= cal->torque_max);
    ok = ok && (cal->derate_start > cal->temp_min_valid);
    ok = ok && (cal->derate_end > cal->derate_start);
    ok = ok && (cal->temp_critical > cal->derate_end);
    ok = ok && (cal->temp_max_valid > cal->temp_critical);
    ok = ok && (cal->derate_min_frac > 0.0f) && (cal->derate_min_frac <= 1.0f);
    ok = ok && (cal->derate_hyst >= 0.0f) && (cal->derate_hyst < 20.0f);
    ok = ok && (cal->limp_frac > 0.0f) && (cal->limp_frac <= 1.0f);
    ok = ok && (cal->limit_slew_down > 0.0f) && (cal->limit_slew_up > 0.0f);
    ok = ok && (cal->safe_ramp > 0.0f);
    ok = ok && (cal->temp_jump > 0.0f) && (cal->temp_filt_tau > 0.0f);
    ok = ok && (cal->speed_max_valid > 0.0f) && (cal->speed_jump > 0.0f);
    ok = ok && (cal->speed_filt_tau > 0.0f);
    ok = ok && (cal->fault_debounce_ticks >= 1u);
    return (ok != 0) ? MC_FAULT_NONE : MC_FAULT_CAL_INVALID;
}

/* ---- Lifecycle ---------------------------------------------------------- */

void MC_Init(MC_Ctx *ctx, const MC_Cal *cal)
{
    ctx->integ           = 0.0f;
    ctx->torque_cmd      = 0.0f;
    ctx->temp_filt       = 0.0f;
    ctx->speed_filt      = 0.0f;
    ctx->temp_fault_cnt  = 0u;
    ctx->speed_fault_cnt = 0u;
    ctx->overtemp_cnt    = 0u;
    ctx->temp_filt_init  = 0u;
    ctx->speed_filt_init = 0u;
    ctx->faults          = MC_CheckCal(cal);
    ctx->cal_ok          = (ctx->faults == MC_FAULT_NONE) ? 1u : 0u;
    ctx->mode            = (ctx->cal_ok != 0u) ? MC_MODE_INIT : MC_MODE_SAFE;
    ctx->torque_limit    = (ctx->cal_ok != 0u) ? cal->torque_max : 0.0f;
}

/* ---- Cyclic task -------------------------------------------------------- */

void MC_Step(MC_Ctx *ctx, const MC_Cal *cal, const MC_Inputs *in, MC_Outputs *out)
{
    uint16_t temp_now  = MC_FAULT_NONE;
    uint16_t speed_now = MC_FAULT_NONE;
    uint16_t overtemp_now;
    uint8_t  derate_active = 0u;
    float    speed_req;
    float    temp_used;
    float    speed_used;
    float    target_limit;

    /* A rejected calibration never runs the control law */
    if (ctx->cal_ok == 0u) {
        ctx->mode         = MC_MODE_SAFE;
        ctx->faults      |= MC_FAULT_CAL_INVALID;
        ctx->integ        = 0.0f;
        ctx->torque_cmd   = 0.0f;
        ctx->torque_limit = 0.0f;
        publish(ctx, 0u, out);
        return;
    }

    /* 1. Sanitise the request: NaN or out-of-range means "request zero" */
    speed_req = in->speed_req;
    if (!((speed_req >= -cal->speed_max_valid) && (speed_req <= cal->speed_max_valid))) {
        speed_req = 0.0f;
    }

    /* 2a. Temperature plausibility */
    if (in->temp_valid == 0u) {
        temp_now = MC_FAULT_TEMP_INVALID;
    } else if (!((in->temp_meas >= cal->temp_min_valid) && (in->temp_meas <= cal->temp_max_valid))) {
        temp_now = MC_FAULT_TEMP_RANGE;
    } else if ((ctx->temp_filt_init != 0u) && (absf(in->temp_meas - ctx->temp_filt) > cal->temp_jump)) {
        temp_now = MC_FAULT_TEMP_JUMP;
    } else {
        /* Plausible: only now may the reading update the filter */
        if (ctx->temp_filt_init != 0u) {
            ctx->temp_filt = lowpass(ctx->temp_filt, in->temp_meas, cal->dt, cal->temp_filt_tau);
        } else {
            ctx->temp_filt      = in->temp_meas;
            ctx->temp_filt_init = 1u;
        }
    }

    /* 2b. Speed plausibility */
    if (in->speed_valid == 0u) {
        speed_now = MC_FAULT_SPEED_INVALID;
    } else if (!((in->speed_meas >= -cal->speed_max_valid) && (in->speed_meas <= cal->speed_max_valid))) {
        speed_now = MC_FAULT_SPEED_RANGE;
    } else if ((ctx->speed_filt_init != 0u) && (absf(in->speed_meas - ctx->speed_filt) > cal->speed_jump)) {
        speed_now = MC_FAULT_SPEED_JUMP;
    } else {
        if (ctx->speed_filt_init != 0u) {
            ctx->speed_filt = lowpass(ctx->speed_filt, in->speed_meas, cal->dt, cal->speed_filt_tau);
        } else {
            ctx->speed_filt      = in->speed_meas;
            ctx->speed_filt_init = 1u;
        }
    }

    /* 2c. Debounce: a fault must persist before it is confirmed and latched */
    ctx->temp_fault_cnt = debounce(ctx->temp_fault_cnt, temp_now, cal->fault_debounce_ticks);
    if (ctx->temp_fault_cnt >= cal->fault_debounce_ticks) {
        ctx->faults |= temp_now;
    }
    ctx->speed_fault_cnt = debounce(ctx->speed_fault_cnt, speed_now, cal->fault_debounce_ticks);
    if (ctx->speed_fault_cnt >= cal->fault_debounce_ticks) {
        ctx->faults |= speed_now;
    }

    /* Values used for control. While a fault is being debounced, fall back to
     * the last plausible (filtered) value. Before any plausible temperature has
     * been seen, assume the worst case: fully derated. */
    if (temp_now == MC_FAULT_NONE) {
        temp_used = in->temp_meas;
    } else if (ctx->temp_filt_init != 0u) {
        temp_used = ctx->temp_filt;
    } else {
        temp_used = cal->derate_end;
    }

    if (speed_now == MC_FAULT_NONE) {
        speed_used = in->speed_meas;
    } else if (ctx->speed_filt_init != 0u) {
        speed_used = ctx->speed_filt;
    } else {
        speed_used = speed_req;  /* zero error: hold, don't push torque blindly */
    }

    /* Over-temperature is only judged on a trusted reading */
    overtemp_now = ((temp_now == MC_FAULT_NONE) && (in->temp_meas >= cal->temp_critical))
                   ? MC_FAULT_OVERTEMP : MC_FAULT_NONE;
    ctx->overtemp_cnt = debounce(ctx->overtemp_cnt, overtemp_now, cal->fault_debounce_ticks);
    if (ctx->overtemp_cnt >= cal->fault_debounce_ticks) {
        ctx->faults |= MC_FAULT_OVERTEMP;
    }

    /* 3. Mode state machine. SAFE and LIMP_HOME latch until MC_Init(). */
    if (ctx->mode != MC_MODE_SAFE) {
        if (((ctx->faults & MC_FAULT_SPEED_MASK) != 0u) || ((ctx->faults & MC_FAULT_OVERTEMP) != 0u)) {
            ctx->mode = MC_MODE_SAFE;           /* cannot control speed safely */
        } else if ((ctx->faults & MC_FAULT_TEMP_MASK) != 0u) {
            ctx->mode = MC_MODE_LIMP_HOME;      /* can drive, but cannot trust temperature */
        } else if (ctx->mode != MC_MODE_LIMP_HOME) {
            if (temp_used >= cal->derate_start) {
                ctx->mode = MC_MODE_DERATED;
            } else if ((ctx->mode == MC_MODE_DERATED) && (temp_used >= (cal->derate_start - cal->derate_hyst))) {
                ctx->mode = MC_MODE_DERATED;    /* hysteresis band: stay derated */
            } else {
                ctx->mode = MC_MODE_NORMAL;
            }
        } else {
            /* LIMP_HOME: latched */
        }
    }

    /* 4. Torque limit, then slew-limit it (fast down, slow up) */
    switch (ctx->mode) {
    case MC_MODE_NORMAL:
    case MC_MODE_DERATED:
        target_limit = cal->torque_max * derate_factor(cal, temp_used);
        break;
    case MC_MODE_LIMP_HOME:
        target_limit = cal->torque_max * cal->limp_frac;
        break;
    case MC_MODE_SAFE:
    case MC_MODE_INIT:
    default:
        target_limit = 0.0f;
        break;
    }

    if (ctx->mode == MC_MODE_SAFE) {
        ctx->torque_limit = approach(ctx->torque_limit, 0.0f, cal->safe_ramp * cal->dt);
    } else if (target_limit < ctx->torque_limit) {
        ctx->torque_limit = approach(ctx->torque_limit, target_limit, cal->limit_slew_down * cal->dt);
    } else {
        ctx->torque_limit = approach(ctx->torque_limit, target_limit, cal->limit_slew_up * cal->dt);
    }
    derate_active = ((ctx->mode == MC_MODE_DERATED) ||
                     (ctx->torque_limit < (cal->torque_max - 0.01f))) ? 1u : 0u;

    /* 5. Speed control */
    if (ctx->mode == MC_MODE_SAFE) {
        ctx->integ      = 0.0f;
        ctx->torque_cmd = approach(ctx->torque_cmd, 0.0f, cal->safe_ramp * cal->dt);
    } else {
        const float lim   = ctx->torque_limit;
        const float err   = speed_req - speed_used;
        const float p     = cal->kp * err;
        const float u_pre = p + ctx->integ;
        float integ_max;

        /* Anti-windup: freeze integration while pushing further into saturation */
        if (!(((u_pre >= lim) && (err > 0.0f)) || ((u_pre <= -lim) && (err < 0.0f)))) {
            ctx->integ += cal->ki * err * cal->dt;
        }
        /* The integrator may never hold more than the current limit allows */
        integ_max       = minf(cal->integ_limit, lim);
        ctx->integ      = clampf(ctx->integ, -integ_max, integ_max);
        ctx->torque_cmd = clampf(p + ctx->integ, -lim, lim);
    }

    /* 6. Outputs */
    publish(ctx, derate_active, out);
}
