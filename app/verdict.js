export const verdictLabel = verdict => verdict === 'healthy' ? 'Not infected' : 'Infected';
export const scoreLabel = result => Number.isFinite(result.disease_score)
  ? `Disease score ${(result.disease_score*100).toFixed(1)}% (uncalibrated; provisional threshold)`
  : `Class score ${(result.confidence*100).toFixed(1)}% (uncalibrated)`;
export const candidateSummary = result => result.diseased_count == null ? verdictLabel(result.verdict)
  : `${result.diseased_count}/${result.leaf_count} flagged`;
