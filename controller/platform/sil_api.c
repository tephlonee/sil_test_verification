/*
 * sil_api.c - Software-in-the-loop platform layer.
 *
 * Exposes the controller as a shared library for the Python harness.
 * This is the ONLY file that changes between platforms: a Raspberry Pi
 * build would replace it with a layer that reads inputs from serial/CAN,
 * calls MC_Step() on a 10 ms timer and writes outputs back.
 */
#include <stddef.h>
#include "motor_ctrl.h"

#if defined(_WIN32)
#define SIL_EXPORT __declspec(dllexport)
#else
#define SIL_EXPORT __attribute__((visibility("default")))
#endif

/* One controller instance per loaded library. The core itself is reentrant. */
static MC_Ctx s_ctx;
static MC_Cal s_cal;

SIL_EXPORT void sil_default_cal(MC_Cal *cal)
{
    MC_GetDefaultCal(cal);
}

/* Pass NULL to use the default calibration. Returns MC_FAULT_NONE or MC_FAULT_CAL_INVALID. */
SIL_EXPORT uint16_t sil_init(const MC_Cal *cal)
{
    if (cal != NULL) {
        s_cal = *cal;
    } else {
        MC_GetDefaultCal(&s_cal);
    }
    MC_Init(&s_ctx, &s_cal);
    return MC_CheckCal(&s_cal);
}

SIL_EXPORT void sil_step(const MC_Inputs *in, MC_Outputs *out)
{
    MC_Step(&s_ctx, &s_cal, in, out);
}

SIL_EXPORT const char *sil_version(void)
{
    return MC_SW_VERSION;
}

/* Lets the Python side verify its ctypes mirrors match the C layout */
SIL_EXPORT void sil_struct_sizes(uint32_t *in_size, uint32_t *out_size, uint32_t *cal_size)
{
    *in_size  = (uint32_t)sizeof(MC_Inputs);
    *out_size = (uint32_t)sizeof(MC_Outputs);
    *cal_size = (uint32_t)sizeof(MC_Cal);
}
