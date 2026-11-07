/*
 * motor_ctrl.h - Motor speed controller with thermal derating.
 *
 * Embedded-style rules followed throughout:
 *   - C99, float32 only (no double), no libm
 *   - no dynamic memory, no recursion, no globals in the core
 *   - all state lives in MC_Ctx, all tunables in MC_Cal (reentrant, testable)
 *   - one cyclic entry point, MC_Step(), called every cal->dt seconds
 *
 * The core knows nothing about Python, CAN or the OS. A platform layer
 * (platform/sil_api.c for SIL, later a Raspberry Pi layer for PIL/HIL)
 * moves data in and out of MC_Inputs / MC_Outputs.
 */
#ifndef MOTOR_CTRL_H
#define MOTOR_CTRL_H

#include <stdint.h>

#define MC_SW_VERSION "0.1.0"

/* Operating modes, in increasing order of severity */
typedef enum {
    MC_MODE_INIT      = 0,
    MC_MODE_NORMAL    = 1,  /* full torque available                        */
    MC_MODE_DERATED   = 2,  /* torque limited by winding temperature        */
    MC_MODE_LIMP_HOME = 3,  /* temperature untrusted: fixed reduced torque  */
    MC_MODE_SAFE      = 4   /* torque ramped to zero, latched until re-init */
} MC_Mode;

/* Fault bits (latched in MC_Ctx.faults once confirmed by debounce) */
#define MC_FAULT_NONE           ((uint16_t)0x0000u)
#define MC_FAULT_TEMP_INVALID   ((uint16_t)0x0001u)  /* sensor flags itself invalid  */
#define MC_FAULT_TEMP_RANGE     ((uint16_t)0x0002u)  /* outside physical range / NaN */
#define MC_FAULT_TEMP_JUMP      ((uint16_t)0x0004u)  /* implausible step change      */
#define MC_FAULT_SPEED_INVALID  ((uint16_t)0x0010u)
#define MC_FAULT_SPEED_RANGE    ((uint16_t)0x0020u)
#define MC_FAULT_SPEED_JUMP     ((uint16_t)0x0040u)
#define MC_FAULT_OVERTEMP       ((uint16_t)0x0100u)  /* winding above critical limit */
#define MC_FAULT_CAL_INVALID    ((uint16_t)0x8000u)  /* calibration failed checks    */

#define MC_FAULT_TEMP_MASK  ((uint16_t)0x000Fu)
#define MC_FAULT_SPEED_MASK ((uint16_t)0x00F0u)

/* ---- Interface ---------------------------------------------------------- */
/* Field order: floats first, then small integers, to keep padding predictable
 * for the ctypes mirror in ctrl_sil.py. */

typedef struct {
    float   speed_req;    /* rad/s, requested speed           */
    float   speed_meas;   /* rad/s, measured speed            */
    float   temp_meas;    /* degC, measured winding temp      */
    uint8_t speed_valid;  /* 1 = sensor reports value valid   */
    uint8_t temp_valid;   /* 1 = sensor reports value valid   */
} MC_Inputs;

typedef struct {
    float    torque_cmd;     /* N*m, torque request to the inverter */
    float    torque_limit;   /* N*m, currently allowed |torque|     */
    uint16_t faults;         /* latched MC_FAULT_* bits              */
    uint8_t  mode;           /* MC_Mode                              */
    uint8_t  derate_active;  /* 1 = torque currently thermally limited */
} MC_Outputs;

/* ---- Calibration (tunable without changing code) ------------------------ */

typedef struct {
    float dt;               /* s, task period                              */
    /* Speed PI controller */
    float kp;               /* N*m / (rad/s)                               */
    float ki;               /* N*m / rad                                   */
    float integ_limit;      /* N*m, integrator clamp                       */
    float torque_max;       /* N*m, absolute torque capability             */
    /* Thermal derating: linear from 100% at derate_start to min_frac at derate_end */
    float derate_start;     /* degC                                        */
    float derate_end;       /* degC                                        */
    float derate_min_frac;  /* 0..1                                        */
    float derate_hyst;      /* K, leave DERATED below derate_start - hyst  */
    float temp_critical;    /* degC, confirmed above this -> SAFE          */
    float limp_frac;        /* 0..1, torque fraction in LIMP_HOME          */
    /* Torque limit slew rates */
    float limit_slew_down;  /* N*m/s, how fast the limit may drop          */
    float limit_slew_up;    /* N*m/s, how fast the limit may recover       */
    float safe_ramp;        /* N*m/s, torque ramp to zero in SAFE          */
    /* Sensor plausibility */
    float temp_min_valid;   /* degC                                        */
    float temp_max_valid;   /* degC                                        */
    float temp_jump;        /* K, max |raw - filtered| before flagging     */
    float temp_filt_tau;    /* s                                           */
    float speed_max_valid;  /* rad/s, |speed| above this is implausible    */
    float speed_jump;       /* rad/s                                       */
    float speed_filt_tau;   /* s                                           */
    uint16_t fault_debounce_ticks;  /* consecutive ticks to confirm a fault */
} MC_Cal;

/* ---- Internal state ---------------------------------------------------- */

typedef struct {
    MC_Mode  mode;
    float    integ;          /* PI integrator, N*m          */
    float    torque_limit;   /* slew-limited limit, N*m     */
    float    torque_cmd;     /* last command, N*m           */
    float    temp_filt;      /* low-passed temperature      */
    float    speed_filt;     /* low-passed speed            */
    uint16_t temp_fault_cnt;
    uint16_t speed_fault_cnt;
    uint16_t overtemp_cnt;
    uint16_t faults;         /* latched fault bits          */
    uint8_t  temp_filt_init;
    uint8_t  speed_filt_init;
    uint8_t  cal_ok;
} MC_Ctx;

/* ---- API ---------------------------------------------------------------- */

void     MC_GetDefaultCal(MC_Cal *cal);
uint16_t MC_CheckCal(const MC_Cal *cal);   /* MC_FAULT_NONE or MC_FAULT_CAL_INVALID */
void     MC_Init(MC_Ctx *ctx, const MC_Cal *cal);
void     MC_Step(MC_Ctx *ctx, const MC_Cal *cal, const MC_Inputs *in, MC_Outputs *out);

#endif /* MOTOR_CTRL_H */
