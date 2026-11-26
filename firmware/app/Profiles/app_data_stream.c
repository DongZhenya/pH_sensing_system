/******************************************************************************

@file  app_data.c

@brief This file contains the Data Stream application functionality.

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
#include <string.h>
#include <stdio.h>
#include <stdbool.h>
#include <time.h>
#include <FreeRTOS.h>
#include <task.h>
#include <ti/drivers/GPIO.h>
#include <ti/ble/profiles/data_stream/data_stream_profile.h>
#include "ti/ble/app_util/framework/bleapputil_api.h"
#include "ti/ble/app_util/menu/menu_module.h"
#include <app_main.h>
#include "ti_drivers_config.h"

//*****************************************************************************
//! Defines
//*****************************************************************************
#define DS_CCC_UPDATE_NOTIFICATION_ENABLED  1

typedef struct
{
  uint8_t channel, gain, sequence;
  uint16_t value;
  uint32_t timeMs, cycle;
} VsenSample;

static volatile bool vsenNotificationsEnabled;
static volatile uint8_t vsenPendingCallbacks;
volatile uint32_t vsenDroppedSamples;
volatile uint32_t vsenSentSamples;
volatile uint8_t vsenLastSendStatus;
static void DS_sendVsenSample(char *data);

//*****************************************************************************
//! Globals
//*****************************************************************************

//*****************************************************************************
//!LOCAL FUNCTIONS
//*****************************************************************************

static void DS_onCccUpdateCB( uint16 connHandle, uint16 pValue );
static void DS_incomingDataCB( uint16 connHandle, char *pValue, uint16 len );

//*****************************************************************************
//!APPLICATION CALLBACK
//*****************************************************************************
// Data Stream application callback function for incoming data
static DSP_cb_t ds_profileCB =
{
  DS_onCccUpdateCB,
  DS_incomingDataCB
};

//*****************************************************************************
//! Functions
//*****************************************************************************
/*********************************************************************
 * @fn      DS_onCccUpdateCB
 *
 * @brief   Callback from Data_Stream_Profile indicating ccc update
 *
 * @param   cccUpdate - pointer to data structure used to store ccc update
 *
 * @return  SUCCESS or stack call status
 */
static void DS_onCccUpdateCB( uint16 connHandle, uint16 pValue )
{
  vsenNotificationsEnabled = (pValue == DS_CCC_UPDATE_NOTIFICATION_ENABLED);
  if (!vsenNotificationsEnabled) { (void)App_setSamplingRunning(false); }
  if ( pValue == DS_CCC_UPDATE_NOTIFICATION_ENABLED)
  {
    MenuModule_printf(APP_MENU_PROFILE_STATUS_LINE, 0,
                      "DataStream status: CCC Update - connectionHandle: "
                      MENU_MODULE_COLOR_YELLOW "%d " MENU_MODULE_COLOR_RESET
                      "Notifications enabled", connHandle);
  }
  else
  {
    MenuModule_printf(APP_MENU_PROFILE_STATUS_LINE, 0,
                      "DataStream status: CCC Update - connectionHandle: "
                      MENU_MODULE_COLOR_YELLOW "%d " MENU_MODULE_COLOR_RESET
                      "Notifications disabled", connHandle);
  }
}

/* Run in the BLE application context. The framework releases data afterward. */
static void DS_sendVsenSample(char *data)
{
  const VsenSample *sample = (const VsenSample *)data;
  uint8_t frame[15];
  uint8_t i;

  taskENTER_CRITICAL();
  if (vsenPendingCallbacks != 0U) { vsenPendingCallbacks--; }
  taskEXIT_CRITICAL();

  if (!vsenNotificationsEnabled)
  {
    return;
  }

  /* Default ATT MTU compatible, little endian. 0xffff = ADC failure. */
  frame[0] = 0xA5U;
  frame[1] = sample->gain;
  frame[2] = sample->sequence;
  frame[3] = 2U;
  frame[4] = sample->channel;
  frame[5] = (uint8_t)sample->value;
  frame[6] = (uint8_t)(sample->value >> 8);
  for (i = 0U; i < 4U; i++)
  {
    frame[7U + i] = (uint8_t)(sample->timeMs >> (8U * i));
    frame[11U + i] = (uint8_t)(sample->cycle >> (8U * i));
  }
  vsenLastSendStatus = DSP_sendData(frame, sizeof(frame));
  if (vsenLastSendStatus == SUCCESS) { vsenSentSamples++; }
  else { vsenDroppedSamples++; }
}

void DataStream_publishSample(uint8_t channel, uint8_t gain, uint8_t sequence,
                              uint16_t value, uint32_t timeMs, uint32_t cycle)
{
  VsenSample *sample;

  if (!vsenNotificationsEnabled)
  {
    return;
  }

  /* Bound heap use if the producer temporarily outpaces the BLE task. */
  taskENTER_CRITICAL();
  if (vsenPendingCallbacks >= 8U)
  {
    vsenDroppedSamples++;
    taskEXIT_CRITICAL();
    return;
  }
  vsenPendingCallbacks++;
  taskEXIT_CRITICAL();
  sample = BLEAppUtil_malloc(sizeof(*sample));
  if (sample != NULL)
  {
    sample->channel = channel;
    sample->gain = gain;
    sample->sequence = sequence;
    sample->value = value;
    sample->timeMs = timeMs;
    sample->cycle = cycle;
    /* Framework frees sample, including enqueue failure. */
    if (BLEAppUtil_invokeFunction(DS_sendVsenSample, (char *)sample) == SUCCESS) { return; }
  }
  taskENTER_CRITICAL();
  vsenPendingCallbacks--;
  vsenDroppedSamples++;
  taskEXIT_CRITICAL();
}

/*********************************************************************
 * @fn      DS_incomingDataCB
 *
 * @brief   Callback from Data_Stream_Profile indicating incoming data
 *
 * @param   dataIn - pointer to data structure used to store incoming data
 *
 * @return  SUCCESS or stack call status
 */
static void DS_incomingDataCB( uint16 connHandle, char *pValue, uint16 len )
{
  uint8_t reply[14];
  const char *error = NULL;
  uint16_t period;
  uint8_t mask, gains[8], i;

  (void)connHandle;
  /* Atomic v2 configuration: C3 02 mask T_lo T_hi gain[8]. */
  if (len == 13U && (uint8_t)pValue[0] == 0xC3U && (uint8_t)pValue[1] == 2U)
  {
    mask = (uint8_t)pValue[2];
    period = (uint16_t)((uint8_t)pValue[3] | ((uint16_t)(uint8_t)pValue[4] << 8));
    for (i = 0U; i < 8U; i++) { gains[i] = (uint8_t)pValue[5U + i]; }
    if (period < 100U || period > 24000U) { error = "ERR,PERIOD"; }
    for (i = 0U; i < 8U; i++)
    {
      if (gains[i] > 3U) { error = "ERR,GAIN"; }
    }
    if (error == NULL && !App_setScanSettings(period, mask, gains))
    {
      error = "ERR,DEBUG"; /* GS0 shares SWDIO during an attached debug session. */
    }
  }
  else if (len == 3U && pValue[0] == 'R' && pValue[1] == '=' &&
           (pValue[2] == '0' || pValue[2] == '1'))
  {
    if (!App_setSamplingRunning(pValue[2] == '1')) { error = "ERR,DEBUG"; }
  }
  else if (!(len == 1U && pValue[0] == 'Q'))
  {
    error = "ERR,FORMAT";
  }

  if (error != NULL)
  {
    (void)DSP_sendData((uint8 *)error, (uint16)strlen(error));
    return;
  }

  App_getScanSettings(&period, &mask, gains);
  reply[0] = 0xC4U;
  reply[1] = 2U;
  reply[2] = mask;
  reply[3] = (uint8_t)period;
  reply[4] = (uint8_t)(period >> 8);
  memcpy(&reply[5], gains, sizeof(gains));
  reply[13] = App_isSamplingRunning() ? 1U : 0U;
  (void)DSP_sendData(reply, sizeof(reply));
}

/*********************************************************************
 * @fn      DataStream_start
 *
 * @brief   This function is called after stack initialization,
 *          the purpose of this function is to initialize and
 *          register the Data Stream profile.
 *
 * @return  SUCCESS or stack call status
 */
bStatus_t DataStream_start( void )
{
  bStatus_t status = SUCCESS;

  status = DSP_start( &ds_profileCB );
  if( status != SUCCESS )
  {
    // Return status value
    return status;
  }

  // Set LEDs
  GPIO_write( CONFIG_GPIO_LED_RED, CONFIG_LED_OFF );
  GPIO_write( CONFIG_GPIO_LED_GREEN, CONFIG_LED_ON );

  return ( SUCCESS );
}
