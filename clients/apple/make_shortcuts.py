#!/usr/bin/env python3
"""Generate the two tailclip Apple Shortcuts ("Send to tailclip" and
"Get from tailclip") for your relay URL.

Shortcuts only imports signed files, so sign them on a Mac:

    python3 make_shortcuts.py https://myserver.tail1234.ts.net
    shortcuts sign -m anyone -i "Send to tailclip.unsigned.shortcut" -o "Send to tailclip.shortcut"
    shortcuts sign -m anyone -i "Get from tailclip.unsigned.shortcut" -o "Get from tailclip.shortcut"
    open "Send to tailclip.shortcut" "Get from tailclip.shortcut"

Imported shortcuts sync to your iPhone and iPad through iCloud.
"""
import argparse
import plistlib
import uuid
from pathlib import Path


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def output(action_uuid: str, name: str) -> dict:
    return {"OutputUUID": action_uuid, "Type": "ActionOutput", "OutputName": name}


def attachment(action_uuid: str, name: str) -> dict:
    return {"Value": output(action_uuid, name), "WFSerializationType": "WFTextTokenAttachment"}


def token_string(action_uuid: str, name: str) -> dict:
    return {
        "Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": output(action_uuid, name)}},
        "WFSerializationType": "WFTextTokenString",
    }


def plain_string(text: str) -> dict:
    return {"Value": {"string": text, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": f"is.workflow.actions.{identifier}", "WFWorkflowActionParameters": params}


def notify(text: str) -> dict:
    return action("notification", WFNotificationActionBody=text, WFNotificationActionSound=False)


SHARE_INPUT_CLASSES = [
    "WFImageContentItem", "WFGenericFileContentItem", "WFPDFContentItem", "WFAVAssetContentItem",
    "WFStringContentItem", "WFRichTextContentItem", "WFURLContentItem", "WFSafariWebPageContentItem",
]


def workflow(actions: list, glyph: int, color: int, **extra) -> dict:
    flow = {
        "WFWorkflowClientVersion": "2607.0.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowActions": actions,
    }
    flow.update(extra)
    return flow


def send_shortcut(base: str) -> dict:
    """Sends whatever was shared to it (several photos or files are sent one
    by one). Run on its own (Back Tap, hotkey) it sends the clipboard."""
    device, loop, post = new_id(), new_id(), new_id()
    shortcut_input = {"Value": {"Type": "ExtensionInput"}, "WFSerializationType": "WFTextTokenAttachment"}
    repeat_item = {"Value": {"Type": "Variable", "VariableName": "Repeat Item"},
                   "WFSerializationType": "WFTextTokenAttachment"}
    return workflow([
        action("getdevicedetails", UUID=device, WFDeviceDetail="Device Model"),
        action("repeat.each", GroupingIdentifier=loop, WFControlFlowMode=0, WFInput=shortcut_input),
        action(
            "downloadurl",
            UUID=post,
            WFURL=f"{base}/api/clips",
            WFHTTPMethod="POST",
            WFHTTPBodyType="File",
            WFRequestVariable=repeat_item,
            ShowHeaders=True,
            WFHTTPHeaders={
                "Value": {"WFDictionaryFieldValueItems": [{
                    "WFItemType": 0,
                    "WFKey": plain_string("X-Source"),
                    "WFValue": token_string(device, "Device Model"),
                }]},
                "WFSerializationType": "WFDictionaryFieldValue",
            },
        ),
        action("repeat.each", GroupingIdentifier=loop, WFControlFlowMode=2, UUID=new_id()),
        notify("Sent to tailclip"),
    ], glyph=59749, color=4282601983,
        WFWorkflowTypes=["ActionExtension", "QuickActions"],
        WFWorkflowInputContentItemClasses=SHARE_INPUT_CLASSES,
        WFWorkflowNoInputBehavior={"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
        WFQuickActionSurfaces=[])


def get_shortcut(base: str) -> dict:
    fetch = new_id()
    return workflow([
        action("downloadurl", UUID=fetch, WFURL=f"{base}/api/latest", WFHTTPMethod="GET"),
        action("setclipboard", WFInput=attachment(fetch, "Contents of URL")),
        notify("Copied from tailclip"),
    ], glyph=59750, color=463140863)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="your relay, e.g. https://myserver.tail1234.ts.net")
    parser.add_argument("-o", "--out", default=".", help="output folder")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, flow in (("Send to tailclip", send_shortcut(base)), ("Get from tailclip", get_shortcut(base))):
        path = out / f"{name}.unsigned.shortcut"
        path.write_bytes(plistlib.dumps(flow, fmt=plistlib.FMT_BINARY))
        print(path)


if __name__ == "__main__":
    main()
