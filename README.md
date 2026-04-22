<p align="center">
<img alt="NivonaCLI" src="img/logo.png" heigth=140 width=140/>
</p>
<h1 align="center">Nivona CLI</h1>
<p align="center">
A small CLI tool that implements all the functions of the <a href="https://play.google.com/store/apps/details?id=de.nivona.mobileapp">Nivona app</a>, to control a coffee machine via BLE.
</p>

## Demo

> [!NOTE]
>
> Currently, only the NICR 660 machine has been tested. With other machines, it may occur that menus do not function correctly or information is displayed incorrectly.

*todo gif*

## Installation

> [!IMPORTANT]
>
> Nivona CLI was developed for Linux and will most likely not run correctly on Windows!

The script only requires the packages `bleak` for the BLE connection and `rich` for the formatted terminal output.

```shell
pip install bleak rich
```

## Misc

### Machine
<p align="center">
  <img src="img/adapter.png" alt="adapter" width="45%" />
  <img src="img/mainboard.jpg" alt="mainboard" width="17.8%" />
</p>

The BLE connection to the coffee machine is established via an adapter board ([EFBTLE16](https://fcc.report/FCC-ID/2AIWT-EFBTLE16/4468692.pdf), FCC-ID: [2AIWT-EFBTLE16](https://fccid.io/2AIWT-EFBTLE16)). This adapter board is a simple PCB housing an [nRF51802](https://www.nordicsemi.com/Products/nRF51802) BLE transceiver chip, which communicates with the coffee machine's main IC ([STM32L100RB](https://www.st.com/en/microcontrollers-microprocessors/stm32l100rb.html)) over UART. While it would be possible to sniff the UART traffic to analyze the BLE packets, this approach would offer limited insight, since the cryptographic operations almost certainly run on the main IC. A far more convenient alternative is to capture the packets directly on the smartphone side.

### App

The Nivona app uses the [Xamarin](https://learn.microsoft.com/en-us/previous-versions/xamarin/android/internals/architecture) platform. This means that the actual program code is written in C#. If you unwrap the assemblies blob, 3 dlls can be identified which are directly relevant for the functionality of the app: **EugsterMobileApp**, **EugsterMobileApp.Droid** and **Eugster.EFLibrary**. The prefix *Eugster* refers to the actual [manufacturer](https://www.eugster.ch/de/) of the device.The two dlls **Arendi.BleLibrary** and **Arendi.DotNETLibrary** provide abstraction APIs for the BLE communication. The prefix *Arendi* refers to the [software contractor](https://www.arendi.ch/).  
The APK not only bundles the Mono runtime to execute the .NET code, but also ships that code obfuscated. This makes dynamic and static code analysis more difficult. Frameworks ([fridax](https://github.com/NorthwaveSecurity/fridax), [mono-api](https://github.com/freehuntx/frida-mono-api)) that simplify hooking with Frida do exist, but they are mostly outdated and require code modifications ([frida-mono-bridge](https://github.com/doyaGu/frida-mono-bridge) seems to work better). 

#### Crypto

Nivona uses its own encryption instead of BLE's native crypto layer, which is why the packets appear encrypted in the HCI log on Android. It uses the symmetric RC4 cipher with a static key that is the same across all machines. A Message `m` can be decrypted as follows:

1. `m[0]` signals the *start (0x53)* and `m[-1]` the *end (0x45)* byte from the raw BLE packet. `m[1:3]` contains the 2-byte ASCII command code (e.g. HR, HX) and is sent in plaintext.
2. The encrypted body is `m[3:-1]`. RC4-decrypt this using the static key to get the plaintext body + checksum.
3. The last byte of the decrypted result is a checksum. Verify by computing `(~sum(command_bytes + body)) & 0xFF` and comparing against the checksum byte.
4. The remaining decrypted bytes are the payload. For post-handshake *requests*, the first 2 bytes are the *session token* established during the HU handshake (strip these to get the actual payload). *Responses* from the machine do not include the *session token*.   

## Credits

[@mpapierski](https://www.github.com/mpapierski) for his AI-driven protocol reversing approach in the [esp-coffee-bridge](https://github.com/mpapierski/esp-coffee-bridge) project!
