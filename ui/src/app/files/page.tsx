"use client";

import { ExternalLink, Upload } from "lucide-react";
import { useEffect, useState } from "react";

import { PageLayout, PageSection } from "@/components/layout/PageLayout";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/lib/auth";

import DocumentList from "./DocumentList";
import DocumentUpload from "./DocumentUpload";

export default function FilesPage() {
    const { user, redirectToLogin, loading } = useAuth();
    const [refreshKey, setRefreshKey] = useState(0);
    const [isUploadOpen, setIsUploadOpen] = useState(false);
    const [uploadBusy, setUploadBusy] = useState(false);

    // Redirect if not authenticated
    useEffect(() => {
        if (!loading && !user) {
            redirectToLogin();
        }
    }, [loading, user, redirectToLogin]);

    const handleUploadSuccess = () => {
        setRefreshKey(prev => prev + 1);
    };

    if (loading || !user) {
        return (
            <PageLayout title="Knowledge Base Files">
                <div className="space-y-4">
                    <Skeleton className="h-12 w-64" />
                    <Skeleton className="h-64 w-full" />
                </div>
            </PageLayout>
        );
    }

    return (
        <PageLayout title="Knowledge Base Files" description={<>Upload and manage documents for your voice agents to reference.{" "}
            <a href="https://docs.dograh.com/voice-agent/knowledge-base" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 underline">
                Learn more <ExternalLink className="h-3 w-3" />
            </a></>}>

            <PageSection title="Your Documents"
                description="Documents shared across all agents in your organization"
                actions={<Button onClick={() => setIsUploadOpen(true)}>
                    <Upload className="w-4 h-4 mr-2" />
                    Upload Documents
                </Button>}>
                <DocumentList refreshTrigger={refreshKey} />
            </PageSection>

            <Dialog open={isUploadOpen} onOpenChange={open => { if (!uploadBusy) setIsUploadOpen(open); }}>
                <DialogContent className="max-h-[85vh] overflow-y-auto">
                    <DialogHeader>
                        <DialogTitle>Upload Documents</DialogTitle>
                        <DialogDescription>
                            Add a batch of documents and track each upload separately.
                        </DialogDescription>
                    </DialogHeader>
                    <DocumentUpload onUploadSuccess={handleUploadSuccess} onBusyChange={setUploadBusy} />
                </DialogContent>
            </Dialog>
        </PageLayout>
    );
}
