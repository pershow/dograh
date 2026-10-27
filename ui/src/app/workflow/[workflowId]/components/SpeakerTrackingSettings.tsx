import { useId } from "react";

import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

export function SpeakerTrackingSettings({ enabled, onChange }: {
    enabled: boolean;
    onChange: (enabled: boolean) => void;
}) {
    const id = useId();
    return (
        <div className="space-y-3">
            <div className="flex items-center justify-between gap-4">
                <Label htmlFor={id} className="text-sm font-semibold">
                    Speaker Tracking (Preview)
                </Label>
                <Switch id={id} checked={enabled} onCheckedChange={onChange} aria-describedby={`${id}-description`} />
            </div>
            <p id={`${id}-description`} className="text-xs text-muted-foreground">
                Automatically estimate the main speaker and speaker changes during each call.
                Temporary voice references are discarded when the call ends. Decisions are saved in call logs for evaluation.
                This preview does not filter speech or change transcription and interruptions.
            </p>
        </div>
    );
}
