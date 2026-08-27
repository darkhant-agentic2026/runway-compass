import { Plus, RotateCcw, Sparkles, Trash2, X } from 'lucide-react';
import { useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import { usePatchLearnerProfile } from '@/features/queries';
import type { LearnerProfile } from '@/lib/schemas';

interface LearnerProfileEditorProps {
  profile: LearnerProfile;
}

const SKILL_LEVELS = ['beginner', 'intermediate', 'advanced', 'expert'] as const;

export function LearnerProfileEditor({ profile }: LearnerProfileEditorProps) {
  const patch = usePatchLearnerProfile();

  // Local state for adding items
  const [newStrength, setNewStrength] = useState('');
  const [newGap, setNewGap] = useState('');
  const [skillName, setSkillName] = useState('');
  const [skillArea, setSkillArea] = useState('');
  const [skillLevel, setSkillLevel] = useState<string>('intermediate');
  const [skillEvidence, setSkillEvidence] = useState('');

  const [thinkingStyle, setThinkingStyle] = useState(profile.thinkingStyle);
  const [pacing, setPacing] = useState(profile.pacing);

  // Keep local fields in sync when profile updates from server
  const [lastVersion, setLastVersion] = useState(profile.version);
  if (profile.version !== lastVersion) {
    setLastVersion(profile.version);
    setThinkingStyle(profile.thinkingStyle);
    setPacing(profile.pacing);
  }

  const handleStartFresh = () => {
    patch.mutate({
      thinkingStyle: '',
      strengths: [],
      gaps: [],
      skills: [],
      pacing: '',
      feedbackNotes: [],
    });
  };

  const handleAddStrength = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newStrength.trim();
    if (!trimmed || profile.strengths.includes(trimmed)) return;
    patch.mutate({ strengths: [...profile.strengths, trimmed] });
    setNewStrength('');
  };

  const handleRemoveStrength = (item: string) => {
    patch.mutate({ strengths: profile.strengths.filter((s) => s !== item) });
  };

  const handleAddGap = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newGap.trim();
    if (!trimmed || profile.gaps.includes(trimmed)) return;
    patch.mutate({ gaps: [...profile.gaps, trimmed] });
    setNewGap('');
  };

  const handleRemoveGap = (item: string) => {
    patch.mutate({ gaps: profile.gaps.filter((g) => g !== item) });
  };

  const handleAddSkill = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = skillName.trim();
    if (!trimmed) return;
    const existing = profile.skills.filter(
      (s) => s.name.toLowerCase() !== trimmed.toLowerCase(),
    );
    patch.mutate({
      skills: [
        ...existing,
        {
          name: trimmed,
          area: skillArea.trim() || 'general',
          level: skillLevel,
          evidence: skillEvidence.trim(),
        },
      ],
    });
    setSkillName('');
    setSkillArea('');
    setSkillEvidence('');
  };

  const handleRemoveSkill = (name: string) => {
    patch.mutate({
      skills: profile.skills.filter((s) => s.name !== name),
    });
  };

  const formattedDate = profile.updatedAt
    ? new Date(profile.updatedAt).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      })
    : null;

  return (
    <Card className="space-y-6">
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Sparkles className="size-5 text-primary" />
            <CardTitle>What your coach knows about you</CardTitle>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={handleStartFresh}
            disabled={patch.isPending}
            className="text-xs"
          >
            <RotateCcw className="mr-1.5 size-3.5" />
            Start fresh
          </Button>
        </div>
        <CardDescription>
          The coach maintains this model across your sessions to adapt guidance style, pacing,
          and depth. You can edit any field or reset it.
        </CardDescription>
        <div className="flex flex-wrap items-center gap-2 pt-1 text-xs text-muted-foreground">
          <Badge variant="secondary">Version {profile.version}</Badge>
          <span>·</span>
          <span>Updated by {profile.updatedBy === 'agent' ? 'Coach' : 'You'}</span>
          {formattedDate && (
            <>
              <span>·</span>
              <span>{formattedDate}</span>
            </>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* Thinking Style */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label htmlFor="thinking-style" className="text-sm font-medium">
              Thinking style
            </Label>
            {profile.thinkingStyle && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setThinkingStyle('');
                  patch.mutate({ thinkingStyle: '' });
                }}
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
              >
                Reset
              </Button>
            )}
          </div>
          <Input
            id="thinking-style"
            placeholder="e.g. Prefers bottom-up learning with concrete code examples before abstract theory"
            maxLength={500}
            value={thinkingStyle}
            onChange={(e) => setThinkingStyle(e.target.value)}
            onBlur={() => {
              if (thinkingStyle !== profile.thinkingStyle) {
                patch.mutate({ thinkingStyle: thinkingStyle.trim() });
              }
            }}
          />
        </div>

        <Separator />

        {/* Strengths */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-sm font-medium">Strengths</Label>
              <p className="text-xs text-muted-foreground">
                Concepts and tools you have mastered
              </p>
            </div>
            {profile.strengths.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => patch.mutate({ strengths: [] })}
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
              >
                Reset
              </Button>
            )}
          </div>

          <div className="flex min-h-6 flex-wrap gap-1.5">
            {profile.strengths.length === 0 ? (
              <span className="text-xs text-muted-foreground italic">None recorded yet</span>
            ) : (
              profile.strengths.map((s) => (
                <Badge key={s} variant="secondary" className="gap-1 pr-1 text-xs">
                  {s}
                  <button
                    type="button"
                    onClick={() => handleRemoveStrength(s)}
                    aria-label={`Remove strength ${s}`}
                    className="rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <X className="size-3" />
                  </button>
                </Badge>
              ))
            )}
          </div>

          <form onSubmit={handleAddStrength} className="flex gap-2">
            <Input
              placeholder="Add a strength (e.g. Python asyncio)"
              value={newStrength}
              onChange={(e) => setNewStrength(e.target.value)}
              className="h-8 text-sm"
            />
            <Button
              type="submit"
              size="sm"
              variant="secondary"
              className="h-8"
              aria-label="Add strength"
              disabled={!newStrength.trim()}
            >
              <Plus className="mr-1 size-3.5" />
              Add
            </Button>
          </form>
        </div>

        <Separator />

        {/* Knowledge Gaps */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-sm font-medium">Areas to reinforce</Label>
              <p className="text-xs text-muted-foreground">
                Topics where you want more practice
              </p>
            </div>
            {profile.gaps.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => patch.mutate({ gaps: [] })}
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
              >
                Reset
              </Button>
            )}
          </div>

          <div className="flex min-h-6 flex-wrap gap-1.5">
            {profile.gaps.length === 0 ? (
              <span className="text-xs text-muted-foreground italic">None recorded yet</span>
            ) : (
              profile.gaps.map((g) => (
                <Badge key={g} variant="outline" className="gap-1 pr-1 text-xs">
                  {g}
                  <button
                    type="button"
                    onClick={() => handleRemoveGap(g)}
                    aria-label={`Remove gap ${g}`}
                    className="rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <X className="size-3" />
                  </button>
                </Badge>
              ))
            )}
          </div>

          <form onSubmit={handleAddGap} className="flex gap-2">
            <Input
              placeholder="Add an area to reinforce (e.g. CSS Grid layouts)"
              value={newGap}
              onChange={(e) => setNewGap(e.target.value)}
              className="h-8 text-sm"
            />
            <Button
              type="submit"
              size="sm"
              variant="secondary"
              className="h-8"
              aria-label="Add area to reinforce"
              disabled={!newGap.trim()}
            >
              <Plus className="mr-1 size-3.5" />
              Add
            </Button>
          </form>
        </div>

        <Separator />

        {/* Skills */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-sm font-medium">Skills</Label>
              <p className="text-xs text-muted-foreground">
                Observed proficiency levels and evidence, each scoped to the subject it was
                observed in — a skill from one subject is not assumed to carry over to another
              </p>
            </div>
            {profile.skills.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => patch.mutate({ skills: [] })}
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
              >
                Reset
              </Button>
            )}
          </div>

          <div className="space-y-2">
            {profile.skills.length === 0 ? (
              <p className="text-xs text-muted-foreground italic">No skills recorded yet</p>
            ) : (
              profile.skills.map((s) => (
                <div
                  key={s.name}
                  className="flex items-center justify-between rounded-md border p-2 text-xs"
                >
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-foreground">{s.name}</span>
                      <Badge variant="outline" className="text-[10px]">
                        {s.area}
                      </Badge>
                      <Badge variant="secondary" className="text-[10px] uppercase">
                        {s.level}
                      </Badge>
                    </div>
                    {s.evidence && (
                      <p className="text-[11px] text-muted-foreground">{s.evidence}</p>
                    )}
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleRemoveSkill(s.name)}
                    aria-label={`Remove skill ${s.name}`}
                    className="size-7 p-0 text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              ))
            )}
          </div>

          <form
            onSubmit={handleAddSkill}
            className="space-y-2 rounded-md border border-dashed p-3"
          >
            <div className="text-xs font-medium text-foreground">Add skill</div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <Input
                placeholder="Skill name (e.g. Type hints)"
                value={skillName}
                onChange={(e) => setSkillName(e.target.value)}
                className="h-8 text-xs"
              />
              <Input
                placeholder="Subject or area (e.g. Python)"
                value={skillArea}
                onChange={(e) => setSkillArea(e.target.value)}
                className="h-8 text-xs"
              />
            </div>
            <Select
              value={skillLevel}
              onValueChange={(val) => {
                if (val) setSkillLevel(val);
              }}
            >
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SKILL_LEVELS.map((lvl) => (
                  <SelectItem key={lvl} value={lvl} className="text-xs">
                    {lvl.charAt(0).toUpperCase() + lvl.slice(1)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              placeholder="Evidence/context (e.g. Built production dashboards)"
              value={skillEvidence}
              onChange={(e) => setSkillEvidence(e.target.value)}
              className="h-8 text-xs"
            />
            <Button
              type="submit"
              size="sm"
              variant="secondary"
              className="h-8 w-full text-xs"
              disabled={!skillName.trim()}
            >
              <Plus className="mr-1 size-3.5" />
              Add skill
            </Button>
          </form>
        </div>

        <Separator />

        {/* Pacing */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label htmlFor="pacing" className="text-sm font-medium">
              Pacing preference
            </Label>
            {profile.pacing && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setPacing('');
                  patch.mutate({ pacing: '' });
                }}
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
              >
                Reset
              </Button>
            )}
          </div>
          <Input
            id="pacing"
            placeholder="e.g. Fast-paced, prefers dense reference material over step-by-step handholding"
            value={pacing}
            onChange={(e) => setPacing(e.target.value)}
            onBlur={() => {
              if (pacing !== profile.pacing) {
                patch.mutate({ pacing: pacing.trim() });
              }
            }}
          />
        </div>

        {/* Recent Session Notes / Observations */}
        {profile.feedbackNotes.length > 0 && (
          <>
            <Separator />
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div>
                  <Label className="text-sm font-medium">Recent session observations</Label>
                  <p className="text-xs text-muted-foreground">
                    Insights and feedback noted by the coach
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => patch.mutate({ feedbackNotes: [] })}
                  className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
                >
                  Clear notes
                </Button>
              </div>
              <ul className="space-y-1.5 rounded-md border p-3 text-xs text-muted-foreground">
                {profile.feedbackNotes.map((note, idx) => (
                  <li key={idx} className="ml-4 list-disc">
                    {note}
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
