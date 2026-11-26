/******************************************************************************

@file  app_main.c

@brief This file contains the application main functionality

Group: WCS, BTS
Target Device: cc23xx

******************************************************************************

 Copyright (c) 2022-2026, Texas Instruments Incorporated
 All rights reserved.

 Redistribution and use in source and binary forms, with or without
 modification, are permitted provided that the following conditions
 are met:

 *  Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

 *  Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in the
    documentation and/or other materials provided with the distribution.

 *  Neither the name of Texas Instruments Incorporated nor the names of
    its contributors may be used to endorse or promote products derived
    from this software without specific prior written permission.

 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
 THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
 CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
 OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
 WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
 OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
 EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

******************************************************************************


*****************************************************************************/

//*****************************************************************************
//! Includes
//*****************************************************************************
#include "ti_ble_config.h"
#include "ti/ble/app_util/framework/bleapputil_api.h"
#include "ti/ble/app_util/menu/menu_module.h"
#include <ti/ble/stack_util/health_toolkit/debugInfo.h>
#include "ti/ble/stack_util/lib_opt/ctrl_stub_ble_health.h"
#include <stdbool.h>
#include <stdint.h>
#include <FreeRTOS.h>
#include <task.h>
#include <ti/drivers/ADC.h>
#include <ti/drivers/GPIO.h>
#include "ti_drivers_config.h"
#include <app_main.h>


//*****************************************************************************
//! Defines
//*****************************************************************************

/* ARM Debug Halting Control and Status Register. Bit 0 is set while a
 * debugger is attached. A1/A2 are SWDIO/SWDCK, so do not reclaim them. */
#define DEBUG_DHCSR_REG       (*((volatile uint32_t *)0xE000EDF0UL))
#define DEBUG_C_DEBUGEN_MASK  (1UL << 0)
#define DEFAULT_SWEEP_PERIOD_MS (1000U)

static volatile uint8_t customPinState;
static volatile uint8_t customGainState;
static volatile uint16_t latestVsenMilliVolts;
static uint8_t latestSampleChannel, latestSampleGain;
static volatile uint16_t sweepPeriodMs = DEFAULT_SWEEP_PERIOD_MS;
static uint8_t channelMask = 0xFFU;
static uint8_t channelGains[8];
static volatile uint32_t settingsGeneration;
static volatile bool samplingRunning;
static TaskHandle_t vsenTaskHandle;
static bool gainPinsAvailable;

static void applyCustomPinState(uint8_t state)
{
    /* C2 is bit 2, B2 is bit 1 and B1 is bit 0. */
    GPIO_write(CONFIG_GPIO_B1_DIO11, state & 1U);
    GPIO_write(CONFIG_GPIO_B2_DIO12, (state >> 1) & 1U);
    GPIO_write(CONFIG_GPIO_C2_DIO8, (state >> 2) & 1U);
}

static void applyGainState(uint8_t state)
{
    /* INA225-Q1 gain sequence by state:
     * G=0: GS0=0 GS1=0 -> 25 V/V
     * G=1: GS0=0 GS1=1 -> 50 V/V
     * G=2: GS0=1 GS1=0 -> 100 V/V
     * G=3: GS0=1 GS1=1 -> 200 V/V */
    GPIO_write(CONFIG_GPIO_INA_GS0_DIO16_SWDIO, (state >> 1) & 1U);
    GPIO_write(CONFIG_GPIO_INA_GS1_DIO24, state & 1U);
}

uint8_t App_getChannelState(void) { return customPinState; }
uint8_t App_getGainState(void) { return customGainState; }
uint16_t App_getVsenMilliVolts(void) { return latestVsenMilliVolts; }
uint16_t App_getSweepPeriodMs(void) { return sweepPeriodMs; }
bool App_isSamplingRunning(void) { return samplingRunning; }

bool App_setSamplingRunning(bool running)
{
    if (running && !gainPinsAvailable) { return false; }
    taskENTER_CRITICAL();
    samplingRunning = running;
    settingsGeneration++;
    taskEXIT_CRITICAL();
    if (vsenTaskHandle != NULL) { xTaskNotifyGive(vsenTaskHandle); }
    return true;
}

void App_getLastSample(uint8_t *channel, uint8_t *gain, uint16_t *value)
{
    taskENTER_CRITICAL();
    *channel = latestSampleChannel;
    *gain = latestSampleGain;
    *value = latestVsenMilliVolts;
    taskEXIT_CRITICAL();
}

void App_getScanSettings(uint16_t *periodMs, uint8_t *mask, uint8_t gains[8])
{
    uint8_t i;
    taskENTER_CRITICAL();
    *periodMs = sweepPeriodMs;
    *mask = channelMask;
    for (i = 0U; i < 8U; i++) { gains[i] = channelGains[i]; }
    taskEXIT_CRITICAL();
}

bool App_setScanSettings(uint16_t periodMs, uint8_t mask, const uint8_t gains[8])
{
    uint8_t i;
    if (periodMs < 100U || periodMs > 24000U || !gainPinsAvailable) { return false; }
    for (i = 0U; i < 8U; i++) { if (gains[i] > 3U) { return false; } }
    taskENTER_CRITICAL();
    sweepPeriodMs = periodMs;
    channelMask = mask;
    for (i = 0U; i < 8U; i++) { channelGains[i] = gains[i]; }
    settingsGeneration++;
    taskEXIT_CRITICAL();
    if (vsenTaskHandle != NULL) { xTaskNotifyGive(vsenTaskHandle); }
    return true;
}

static void configureCustomBoardPins(void)
{
    /* YBG B3 = DIO18, YBG A4 = DIO21. */
    GPIO_write(CONFIG_GPIO_B3_DIO18, 1);
    GPIO_write(CONFIG_GPIO_A4_DIO21, 0);

    /* YBG A2 = DIO17/SWDCK. Reclaim it only during standalone operation. */
    if ((DEBUG_DHCSR_REG & DEBUG_C_DEBUGEN_MASK) == 0U)
    {
        GPIO_setConfig(CONFIG_GPIO_A2_DIO17_SWDCK,
                       GPIO_CFG_OUT_STD | GPIO_CFG_OUT_HIGH);
        GPIO_write(CONFIG_GPIO_A2_DIO17_SWDCK, 1);
    }

    /* Start at CH=000, G=00; the phone controls gain and sweep interval. */
    customPinState = 0U;
    applyCustomPinState(customPinState);
    customGainState = 0U;
    latestVsenMilliVolts = 0U;
    sweepPeriodMs = DEFAULT_SWEEP_PERIOD_MS;
    gainPinsAvailable = ((DEBUG_DHCSR_REG & DEBUG_C_DEBUGEN_MASK) == 0U);
    if (gainPinsAvailable)
    {
        GPIO_setConfig(CONFIG_GPIO_INA_GS0_DIO16_SWDIO,
                       GPIO_CFG_OUT_STD | GPIO_CFG_OUT_LOW);
        applyGainState(customGainState);
    }
    else
    {
        GPIO_write(CONFIG_GPIO_INA_GS1_DIO24, 0);
    }

}

/* Apply channel/gain, wait T/N, convert and publish, then select next channel.
 * A settings update aborts the pending wait and starts a fresh cycle, so no
 * reading is labelled with gain/mask settings that changed during settling. */
static void vsenSampleTask(void *arg)
{
    ADC_Params params;
    ADC_Handle adc;
    uint16_t raw;
    uint8_t channel;
    uint8_t gains[8];
    uint8_t mask;
    uint8_t count;
    uint8_t index;
    bool running;
    uint8_t sequence = 0U;
    uint16_t period;
    uint16_t value;
    uint32_t generation;
    uint32_t cycle = 0U;
    uint32_t timeMs;
    uint32_t microVolts;
    TickType_t periodTicks;
    TickType_t waitTicks;
    TickType_t lastAdvUpdate = 0U;

    (void)arg;
    ADC_init();
    ADC_Params_init(&params);
    adc = ADC_open(CONFIG_ADC_VSEN, &params);
    if (adc == NULL)
    {
        vTaskDelete(NULL);
        return;
    }

    for (;;)
    {
        /* Snapshot settings and clear the wake flag as one operation. */
        taskENTER_CRITICAL();
        (void)ulTaskNotifyTake(pdTRUE, 0U);
        generation = settingsGeneration;
        running = samplingRunning;
        App_getScanSettings(&period, &mask, gains);
        taskEXIT_CRITICAL();
        count = 0U;
        for (channel = 0U; channel < 8U; channel++)
        {
            if ((mask & (1U << channel)) != 0U) { count++; }
        }
        if (!running || count == 0U)
        {
            (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        cycle++;
        periodTicks = pdMS_TO_TICKS(period);
        index = 0U;
        for (channel = 0U; channel < 8U; channel++)
        {
            if ((mask & (1U << channel)) == 0U) { continue; }
            customPinState = channel;
            applyCustomPinState(channel);
            customGainState = gains[channel];
            if (gainPinsAvailable) { applyGainState(customGainState); }
            /* Distribute tick remainder: sum of all waits equals T exactly. */
            waitTicks = periodTicks / count + (index < periodTicks % count ? 1U : 0U);
            index++;
            if (ulTaskNotifyTake(pdTRUE, waitTicks) != 0U) { break; }
            if (generation != settingsGeneration) { break; }
            if (ADC_convert(adc, &raw) == ADC_STATUS_SUCCESS)
            {
                microVolts = ADC_convertToMicroVolts(adc, raw);
                value = (uint16_t)((microVolts + 500U) / 1000U);
            }
            else { value = 0xFFFFU; }
            if (generation != settingsGeneration) { break; }
            taskENTER_CRITICAL();
            latestSampleChannel = channel;
            latestSampleGain = customGainState;
            latestVsenMilliVolts = value;
            taskEXIT_CRITICAL();
            {
                TickType_t now = xTaskGetTickCount();
                timeMs = (uint32_t)((uint64_t)now * 1000U / configTICK_RATE_HZ);
            }
            DataStream_publishSample(channel, customGainState, sequence++, value, timeMs, cycle);
            if ((xTaskGetTickCount() - lastAdvUpdate) >= pdMS_TO_TICKS(1000U))
            {
                Peripheral_requestAdvUpdate();
                lastAdvUpdate = xTaskGetTickCount();
            }
        }
    }
}

//*****************************************************************************
//! Globals
//*****************************************************************************

// Parameters that should be given as input to the BLEAppUtil_init function
BLEAppUtil_GeneralParams_t appMainParams =
{
    .taskPriority = 1,
    .taskStackSize = 1024,
    .profileRole = (BLEAppUtil_Profile_Roles_e)(HOST_CONFIG),
    .addressMode = DEFAULT_ADDRESS_MODE,
    .deviceNameAtt = attDeviceName,
    .pDeviceRandomAddress = pRandomAddress,
};

BLEAppUtil_PeriCentParams_t appMainPeriCentParams =
{
#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( PERIPHERAL_CFG ) )
 .connParamUpdateDecision = DEFAULT_PARAM_UPDATE_REQ_DECISION,
#endif

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( PERIPHERAL_CFG | CENTRAL_CFG ) )
#ifdef GAP_BOND_MGR
 .gapBondParams = &gapBondParams
#endif //GAP_BOND_MGR
#endif
};

//*****************************************************************************
//! Functions
//*****************************************************************************

/*********************************************************************
 * @fn      criticalErrorHandler
 *
 * @brief   Application task entry point
 *
 * @return  none
 */
void criticalErrorHandler(int32 errorCode , void* pInfo)
{
//    trace();
//
//#ifdef DEBUG_ERR_HANDLE
//
//    while (1);
//#else
//    SystemReset();
//#endif

}

/*********************************************************************
 * @fn      App_StackInitDone
 *
 * @brief   This function will be called when the BLE stack init is
 *          done.
 *          It should call the applications modules start functions.
 *
 * @return  none
 */
void App_StackInitDoneHandler(gapDeviceInitDoneEvent_t *deviceInitDoneData)
{
    bStatus_t status = SUCCESS;

    // Print the device ID address
    MenuModule_printf(APP_MENU_DEVICE_ADDRESS, 0, "BLE ID Address: "
                      MENU_MODULE_COLOR_BOLD MENU_MODULE_COLOR_GREEN "%s" MENU_MODULE_COLOR_RESET,
                      BLEAppUtil_convertBdAddr2Str(deviceInitDoneData->devAddr));

    if ( appMainParams.addressMode > ADDRMODE_RANDOM)
    {
      // Print the RP address
        MenuModule_printf(APP_MENU_DEVICE_RP_ADDRESS, 0,
                     "BLE RP Address: "
                     MENU_MODULE_COLOR_BOLD MENU_MODULE_COLOR_GREEN "%s" MENU_MODULE_COLOR_RESET,
                     BLEAppUtil_convertBdAddr2Str(GAP_GetDevAddress(FALSE)));
    }

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( PERIPHERAL_CFG | CENTRAL_CFG ) )
    status = DevInfo_start();
    if ( status != SUCCESS )
    {
    // TODO: Call Error Handler
    }
    status = DataStream_start();
    if ( status != SUCCESS )
    {
    // TODO: Call Error Handler
    }
#endif

    status = OPT_DbgInf_init( DBGINF_DOMAIN_ALL );
    if ( status != SUCCESS )
    {
    // TODO: Call Error Handler
    }

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( PERIPHERAL_CFG ) )
    // Any device that accepts the establishment of a link using
    // any of the connection establishment procedures referred to
    // as being in the Peripheral role.
    // A device operating in the Peripheral role will be in the
    // Peripheral role in the Link Layer Connection state.
    status = Peripheral_start();
    if(status != SUCCESS)
    {
    // TODO: Call Error Handler
    }
#endif

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( BROADCASTER_CFG ) )
    // A device operating in the Broadcaster role is a device that
    // sends advertising events or periodic advertising events
    status = Broadcaster_start();
    if(status != SUCCESS)
    {
    // TODO: Call Error Handler
    }
#endif

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( CENTRAL_CFG ) )
    // A device that supports the Central role initiates the establishment
    // of an active physical link. A device operating in the Central role will
    // be in the Central role in the Link Layer Connection state.
    // A device operating in the Central role is referred to as a Central.
    status = Central_start();
    if(status != SUCCESS)
    {
    // TODO: Call Error Handler
    }
#endif

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( OBSERVER_CFG ) )
    // A device operating in the Observer role is a device that
    // receives advertising events or periodic advertising events
    status = Observer_start();
    if(status != SUCCESS)
    {
    // TODO: Call Error Handler
    }
#endif

#if defined( HOST_CONFIG ) && ( HOST_CONFIG & ( PERIPHERAL_CFG | CENTRAL_CFG ) )
    status = Connection_start();
    if ( status != SUCCESS )
    {
    // TODO: Call Error Handler
    }
    status = Pairing_start();
    if ( status != SUCCESS )
    {
    // TODO: Call Error Handler
    }
    status = Data_start();
    if ( status != SUCCESS )
    {
    // TODO: Call Error Handler
    }
#endif
}

/*********************************************************************
 * @fn      appMain
 *
 * @brief   Application main function
 *
 * @return  none
 */
void appMain(void)
{
    configureCustomBoardPins();

    (void)xTaskCreate(vsenSampleTask, "VsenADC", 768, NULL, 1,
                      &vsenTaskHandle);

#if !defined(Display_DISABLE_ALL)
    MenuModule_params_t params = {
      .mode = MenuModule_Mode_PRINTS_ONLY
    };

    if(MenuModule_init(NULL, &params) != SUCCESS)
    {
    // TODO: Call Error Handler
    }
#endif // #if !defined(Display_DISABLE_ALL)

    // Print the application name
    MenuModule_printf(APP_MENU_GENERAL_STATUS_LINE, 0,
                      MENU_MODULE_COLOR_BOLD MENU_MODULE_COLOR_CYAN
                      "Data Stream" MENU_MODULE_COLOR_RESET);

    // Call the BLEAppUtil module init function
    BLEAppUtil_init(&criticalErrorHandler, &App_StackInitDoneHandler,
                    &appMainParams, &appMainPeriCentParams);
}
